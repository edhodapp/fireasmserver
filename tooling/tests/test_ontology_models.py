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
    SideSessionTask,
    VerificationCase,
    validate_ontology_strict,
)


_VALID_TS = "2026-04-20T12:00:00+00:00"


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


class TestModuleSpecDependencySplit:
    """2026-04-21 split: ``internal_module_refs`` is
    ``list[SafeId]`` (cross-ref checked at Ontology level);
    ``external_imports`` is free-form with string hygiene."""

    def test_accepts_clean_split(self) -> None:
        mod = ModuleSpec(
            name="m",
            responsibility="r",
            internal_module_refs=["vm_launcher"],
            external_imports=["subprocess", "pathlib"],
        )
        assert mod.internal_module_refs == ["vm_launcher"]
        assert len(mod.external_imports) == 2

    def test_accepts_empty_lists(self) -> None:
        mod = ModuleSpec(name="m", responsibility="r")
        assert not mod.internal_module_refs
        assert not mod.external_imports

    def test_rejects_empty_external_import(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ModuleSpec(
                name="m", responsibility="r",
                external_imports=["pathlib", ""],
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
    def test_external_imports_reject_whitespace(
        self, bad: str,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            ModuleSpec(
                name="m", responsibility="r",
                external_imports=[bad],
            )
        assert "whitespace" in str(exc.value)

    def test_rejects_duplicate_external_imports(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ModuleSpec(
                name="m", responsibility="r",
                external_imports=["pathlib", "subprocess", "pathlib"],
            )
        assert "duplicate" in str(exc.value)
        assert "'pathlib'" in str(exc.value)

    def test_rejects_duplicate_internal_refs(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ModuleSpec(
                name="m", responsibility="r",
                internal_module_refs=["vm_launcher", "vm_launcher"],
            )
        assert "duplicate" in str(exc.value)
        assert "'vm_launcher'" in str(exc.value)

    def test_internal_refs_enforce_safeid_shape(self) -> None:
        """SafeId blocks malformed refs at the type layer — a
        leading-dash or whitespace-containing ref is rejected
        before it can reach the sibling-resolution check."""
        with pytest.raises(ValidationError):
            ModuleSpec(
                name="m", responsibility="r",
                internal_module_refs=["-leading-dash"],
            )
        with pytest.raises(ValidationError):
            ModuleSpec(
                name="m", responsibility="r",
                internal_module_refs=["has space"],
            )


class TestOntologyInternalModuleRefRI:
    """Every ``internal_module_refs`` entry must resolve to
    another declared ``ModuleSpec.name`` — the reverse arrow the
    split was introduced to make checkable."""

    def test_resolved_ref_accepted(self) -> None:
        Ontology(modules=[
            ModuleSpec(name="vm_launcher", responsibility="r"),
            ModuleSpec(
                name="test_runner", responsibility="r",
                internal_module_refs=["vm_launcher"],
            ),
        ])

    def test_dangling_ref_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(modules=[
                ModuleSpec(
                    name="test_runner", responsibility="r",
                    internal_module_refs=["ghost_module"],
                ),
            ])
        message = str(exc.value)
        assert "test_runner" in message
        assert "'ghost_module'" in message
        assert "not in modules" in message

    def test_dangling_internal_ref_joins_other_ri_errors(
        self,
    ) -> None:
        """Dangling module refs surface alongside other RI
        errors in one validation pass, per the established
        all-errors-together pattern."""
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[_entity("a")],
                modules=[ModuleSpec(
                    name="m", responsibility="r",
                    internal_module_refs=["ghost_module"],
                )],
                relationships=[Relationship(
                    source_entity_id="ghost_entity",
                    target_entity_id="a",
                    name="r", cardinality="one_to_one",
                )],
            )
        message = str(exc.value)
        assert "'ghost_module'" in message
        assert "'ghost_entity'" in message


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


# ---------------------------------------------------------------
# Decision.chosen ∈ Decision.options.
# ---------------------------------------------------------------


class TestDecisionChosenAmongOptions:
    """``Decision.chosen`` MUST be one of ``Decision.options`` —
    a record that picks an option that wasn't on the list
    represents an impossible state."""

    def test_accepts_chosen_in_options(self) -> None:
        d = Decision(
            question="q",
            options=["A", "B", "C"],
            chosen="B",
            rationale="r",
        )
        assert d.chosen == "B"

    def test_rejects_chosen_not_in_options(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Decision(
                question="q",
                options=["A", "B"],
                chosen="C",
                rationale="r",
            )
        message = str(exc.value)
        assert "'C'" in message
        assert "options" in message

    def test_rejects_empty_options_with_any_chosen(self) -> None:
        with pytest.raises(ValidationError):
            Decision(
                question="q",
                options=[],
                chosen="anything",
                rationale="r",
            )


# ---------------------------------------------------------------
# IsoTimestamp — applied to DAGNode.created_at / DAGEdge.created_at.
# ---------------------------------------------------------------


class TestIsoTimestampOnDagNode:
    """``DAGNode.created_at`` is now ``IsoTimestamp`` — same
    two-layer validation as ``IsoDate`` (regex + parse)."""

    @pytest.mark.parametrize("ts", [
        "2026-04-20T12:00:00",
        "2026-04-20T12:00:00Z",
        "2026-04-20T12:00:00+00:00",
        "2026-04-20T12:00:00-07:00",
        "2026-04-20T12:00:00.123456+00:00",
    ])
    def test_accepts_iso_timestamp(self, ts: str) -> None:
        node = DAGNode(
            id="n",
            ontology=Ontology(),
            created_at=ts,
        )
        assert node.created_at == ts

    @pytest.mark.parametrize("bad", [
        "2026-04-20",                   # date only
        "2026-04-20 12:00:00",          # space separator
        "26-04-20T12:00:00",            # two-digit year
        "not-a-timestamp",
        "",
        "2026-02-30T12:00:00",          # impossible day
        "2026-04-20T25:00:00",          # hour 25
    ])
    def test_rejects_bad_timestamp(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            DAGNode(
                id="n",
                ontology=Ontology(),
                created_at=bad,
            )


class TestIsoTimestampFractionalSeconds:
    """Pin the accepted set of fractional-second widths so a
    future Python upgrade can't silently change behavior. The
    regex allows ``\\.\\d+`` (any width); ``datetime.fromisoformat``
    in Python 3.11+ accepts most widths. Tests lock the
    intersection."""

    @pytest.mark.parametrize("frac", ["1", "12", "123", "123456", "1234567"])
    def test_accepts_various_fractional_widths(
        self, frac: str,
    ) -> None:
        ts = f"2026-04-20T12:00:00.{frac}+00:00"
        node = DAGNode(id="n", ontology=Ontology(), created_at=ts)
        assert node.created_at == ts


class TestIsoTimestampOnDagEdge:
    """Same ``IsoTimestamp`` contract applied to ``DAGEdge.created_at``."""

    def test_applies_to_edges_too(self) -> None:
        edge = DAGEdge(
            parent_id="A",
            child_id="B",
            decision=Decision(
                question="q", options=["x"], chosen="x", rationale="r",
            ),
            created_at=_VALID_TS,
        )
        assert edge.created_at == _VALID_TS

    def test_rejects_bad_timestamp_on_edge(self) -> None:
        with pytest.raises(ValidationError):
            DAGEdge(
                parent_id="A",
                child_id="B",
                decision=Decision(
                    question="q", options=["x"], chosen="x",
                    rationale="r",
                ),
                created_at="not-a-timestamp",
            )


# ---------------------------------------------------------------
# SafeId tightening on cross-reference fields. Malformed IDs now
# fail at construction time, in addition to the existing RI check
# that catches dangling-but-well-formed references.
# ---------------------------------------------------------------


class TestSafeIdOnCrossReferences:
    """Cross-reference fields (source/target/entity_ids) are now
    ``SafeId`` — malformed ids caught at construction, not only
    via the downstream RI check."""

    @pytest.mark.parametrize("bad", [
        "-leading-dash", "has space", "with/slash", ".",
    ])
    def test_relationship_source_rejects_malformed(
        self, bad: str,
    ) -> None:
        with pytest.raises(ValidationError):
            Relationship(
                source_entity_id=bad,
                target_entity_id="b",
                name="r",
                cardinality="one_to_one",
            )

    @pytest.mark.parametrize("bad", [
        "-leading-dash", "has space", "with/slash",
    ])
    def test_relationship_target_rejects_malformed(
        self, bad: str,
    ) -> None:
        with pytest.raises(ValidationError):
            Relationship(
                source_entity_id="a",
                target_entity_id=bad,
                name="r",
                cardinality="one_to_one",
            )

    def test_domain_constraint_entity_ids_reject_malformed(self) -> None:
        with pytest.raises(ValidationError):
            DomainConstraint(
                name="dc",
                description="d",
                entity_ids=["valid_one", "has space"],
            )

    def test_performance_constraint_entity_ids_reject_malformed(
        self,
    ) -> None:
        with pytest.raises(ValidationError):
            PerformanceConstraint(
                name="pc",
                description="d",
                entity_ids=["-leading-dash"],
                metric="m",
                budget=1.0,
                unit="u",
                direction="min",
            )

    def test_data_model_entity_id_rejects_malformed(self) -> None:
        with pytest.raises(ValidationError):
            DataModel(
                entity_id="has space",
                storage="mem",
                class_name="C",
            )

    def test_well_formed_ids_still_accepted(self) -> None:
        """The tightening must not break the ids already in use
        across every committed DAG node. Representative sample of
        slug shapes the existing data contains."""
        for good in [
            "guest-image", "vm-instance", "ethernet-frame",
            "test-case", "no-native-execution", "a",
        ]:
            Relationship(
                source_entity_id=good,
                target_entity_id="b",
                name="r",
                cardinality="one_to_one",
            )


class TestSafeIdOnDagIds:
    """``DAGNode.id``, ``DAGEdge.parent_id`` / ``child_id``, and
    ``OntologyDAG.current_node_id`` are also cross-reference ids
    and carry the same ``SafeId`` shape requirement — the
    committed DAG uses UUID strings (``1c03a47b-abe2-...``) which
    match SafeId cleanly."""

    @pytest.mark.parametrize("bad", [
        "-leading-dash", "has space", "with/slash",
    ])
    def test_dag_node_id_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            DAGNode(
                id=bad,
                ontology=Ontology(),
                created_at=_VALID_TS,
            )

    def test_dag_edge_parent_id_rejects_malformed(self) -> None:
        with pytest.raises(ValidationError):
            DAGEdge(
                parent_id="has space",
                child_id="b",
                decision=Decision(
                    question="q", options=["x"], chosen="x",
                    rationale="r",
                ),
                created_at=_VALID_TS,
            )

    def test_dag_edge_child_id_rejects_malformed(self) -> None:
        with pytest.raises(ValidationError):
            DAGEdge(
                parent_id="a",
                child_id="-leading",
                decision=Decision(
                    question="q", options=["x"], chosen="x",
                    rationale="r",
                ),
                created_at=_VALID_TS,
            )

    def test_uuid_shaped_ids_accepted(self) -> None:
        """The committed DAG's real ids are UUIDs. Double-check
        the tightened rule still accepts them."""
        uuid_like = "1c03a47b-abe2-4c44-aaf3-408285ddebef"
        DAGNode(id=uuid_like, ontology=Ontology(), created_at=_VALID_TS)

    def test_current_node_id_empty_is_the_sentinel(self) -> None:
        """Empty ``current_node_id`` means "no current node" —
        it's the fresh-DAG default and MUST pass validation."""
        dag = OntologyDAG(project_name="p")
        assert dag.current_node_id == ""

    def test_current_node_id_rejects_malformed_when_non_empty(
        self,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            OntologyDAG(project_name="p", current_node_id="has space")
        assert "current_node_id" in str(exc.value)

    def test_current_node_id_accepts_valid_safeid(self) -> None:
        dag = OntologyDAG(
            project_name="p",
            current_node_id="1c03a47b-abe2-4c44-aaf3-408285ddebef",
        )
        assert dag.current_node_id.startswith("1c03a47b")

    def test_current_node_id_rejects_over_100_chars(self) -> None:
        """Even if the regex matches, a non-empty
        ``current_node_id`` must honor ``SafeId``'s
        ``max_length=100`` bound. A valid-shaped id longer than
        that would silently bypass length checks elsewhere."""
        with pytest.raises(ValidationError) as exc:
            OntologyDAG(project_name="p", current_node_id="a" * 101)
        assert "current_node_id" in str(exc.value)


class TestConstraintNameUniqueness:
    """``DomainConstraint.name`` and ``PerformanceConstraint.name``
    are used as identifiers in the RI error messages, so
    duplicates within an ontology produce ambiguous diagnostics.
    The uniqueness check spans both lists — a domain constraint
    named the same as a performance constraint is also a clash."""

    def test_unique_names_accepted(self) -> None:
        Ontology(
            entities=[_entity("a")],
            domain_constraints=[
                DomainConstraint(
                    name="dc1", description="", entity_ids=["a"],
                ),
                DomainConstraint(
                    name="dc2", description="", entity_ids=["a"],
                ),
            ],
            performance_constraints=[PerformanceConstraint(
                name="pc1", description="", entity_ids=["a"],
                metric="m", budget=1.0, unit="u", direction="min",
            )],
        )

    def test_duplicate_domain_constraint_name_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[_entity("a")],
                domain_constraints=[
                    DomainConstraint(
                        name="dup", description="", entity_ids=["a"],
                    ),
                    DomainConstraint(
                        name="dup", description="", entity_ids=["a"],
                    ),
                ],
            )
        assert "'dup'" in str(exc.value)
        assert "not unique" in str(exc.value)

    def test_duplicate_across_kinds_rejected(self) -> None:
        """A DomainConstraint and a PerformanceConstraint
        sharing the same name also counts as a collision —
        error messages can't distinguish them."""
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[_entity("a")],
                domain_constraints=[DomainConstraint(
                    name="shared", description="", entity_ids=["a"],
                )],
                performance_constraints=[PerformanceConstraint(
                    name="shared", description="", entity_ids=["a"],
                    metric="m", budget=1.0, unit="u", direction="min",
                )],
            )
        assert "'shared'" in str(exc.value)
        assert "not unique" in str(exc.value)


class TestEntityIdUniqueness:
    """Entity ids are the keys for every cross-reference check —
    duplicates collapse silently in the ``known`` set used by RI
    helpers, so they MUST be flagged at the ontology level."""

    def test_unique_ids_accepted(self) -> None:
        Ontology(entities=[_entity("a"), _entity("b"), _entity("c")])

    def test_duplicate_id_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(entities=[_entity("dup"), _entity("dup")])
        assert "'dup'" in str(exc.value)
        assert "not unique" in str(exc.value)


class TestModuleNameUniqueness:
    """``ModuleSpec.name`` is the key for internal-module
    references in ``ModuleSpec.dependencies``."""

    def test_unique_module_names_accepted(self) -> None:
        Ontology(modules=[
            ModuleSpec(name="m1", responsibility="r1"),
            ModuleSpec(name="m2", responsibility="r2"),
        ])

    def test_duplicate_module_name_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(modules=[
                ModuleSpec(name="dup", responsibility="r1"),
                ModuleSpec(name="dup", responsibility="r2"),
            ])
        assert "'dup'" in str(exc.value)
        assert "ModuleSpec name" in str(exc.value)


class TestSideSessionTaskUniqueness:
    """``(slug, date)`` is the documented uniqueness key for
    ``SideSessionTask`` — the bootstrap tool's duplicate-check
    relies on it; the ontology must enforce it too so a
    hand-edited DAG can't ship a duplicate."""

    def test_unique_slug_date_accepted(self) -> None:
        Ontology(side_session_tasks=[
            SideSessionTask(
                slug="task_a", date="2026-04-20", deliverables="d",
            ),
            SideSessionTask(
                slug="task_b", date="2026-04-20", deliverables="d",
            ),
            SideSessionTask(
                slug="task_a", date="2026-04-21", deliverables="d",
            ),
        ])

    def test_duplicate_slug_date_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(side_session_tasks=[
                SideSessionTask(
                    slug="dup", date="2026-04-20", deliverables="d",
                ),
                SideSessionTask(
                    slug="dup", date="2026-04-20",
                    deliverables="d2",
                ),
            ])
        assert "'dup'" in str(exc.value)
        assert "2026-04-20" in str(exc.value)
        assert "not unique" in str(exc.value)


# ---------------------------------------------------------------
# VerificationCase — SysE-traceability test records
# ---------------------------------------------------------------


def _constraint(name: str) -> DomainConstraint:
    """Helper — a minimal DomainConstraint to anchor
    ``covers`` references in tests."""
    return DomainConstraint(name=name, description="d", entity_ids=[])


class TestVerificationCaseConstruction:
    """Minimum required fields and defaults."""

    def test_planned_accepts_no_refs(self) -> None:
        vc = VerificationCase(
            name="eth-layout-minimal",
            covers=["eth-frame-layout"],
            tier="A",
        )
        assert vc.status == "planned"
        assert not vc.implementation_refs

    @pytest.mark.parametrize("tier", ["A", "B", "C", "D"])
    def test_all_tiers_accepted(self, tier: str) -> None:
        vc = VerificationCase(
            name=f"t-{tier.lower()}",
            covers=["c"],
            tier=tier,  # type: ignore[arg-type]
        )
        assert vc.tier == tier

    @pytest.mark.parametrize("bad_tier", ["E", "a", "", "1"])
    def test_bad_tier_rejected(self, bad_tier: str) -> None:
        with pytest.raises(ValidationError):
            VerificationCase(
                name="x", covers=["c"],
                tier=bad_tier,  # type: ignore[arg-type]
            )


class TestVerificationCaseStatusContract:
    """Status-dependent rules: written/passing require
    implementation_refs; superseded requires rationale."""

    @pytest.mark.parametrize("status", ["written", "passing"])
    def test_written_passing_without_refs_rejected(
        self, status: str,
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            VerificationCase(
                name="x", covers=["c"], tier="A",
                status=status,  # type: ignore[arg-type]
            )
        assert "implementation_refs" in str(exc.value)

    @pytest.mark.parametrize("status", ["written", "passing"])
    def test_written_passing_with_refs_accepted(
        self, status: str,
    ) -> None:
        vc = VerificationCase(
            name="x", covers=["c"], tier="A",
            status=status,  # type: ignore[arg-type]
            implementation_refs=[
                "tooling/tests/test_foo.py:test_x",
            ],
        )
        assert vc.status == status

    def test_superseded_without_rationale_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            VerificationCase(
                name="x", covers=["c"], tier="A",
                status="superseded",
            )
        assert "rationale" in str(exc.value)

    def test_superseded_with_rationale_accepted(self) -> None:
        vc = VerificationCase(
            name="x", covers=["c"], tier="A",
            status="superseded",
            rationale="replaced by y after spec change",
        )
        assert vc.status == "superseded"

    def test_planned_never_requires_refs_or_rationale(self) -> None:
        """planned is the default; no status-dependent
        obligations fire."""
        VerificationCase(name="x", covers=["c"], tier="A")


class TestVerificationCaseNameUniqueness:
    """Each ``VerificationCase.name`` is the test's identifier
    in TEST_PLAN.md; duplicates make RI error messages
    ambiguous."""

    def test_unique_names_accepted(self) -> None:
        Ontology(
            entities=[Entity(id="a", name="A")],
            domain_constraints=[_constraint("c")],
            verification_cases=[
                VerificationCase(name="t1", covers=["c"], tier="A"),
                VerificationCase(name="t2", covers=["c"], tier="A"),
            ],
        )

    def test_duplicate_name_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[Entity(id="a", name="A")],
                domain_constraints=[_constraint("c")],
                verification_cases=[
                    VerificationCase(
                        name="dup", covers=["c"], tier="A",
                    ),
                    VerificationCase(
                        name="dup", covers=["c"], tier="B",
                    ),
                ],
            )
        assert "'dup'" in str(exc.value)
        assert "VerificationCase" in str(exc.value)


class TestVerificationCaseCoversReferentialIntegrity:
    """``covers`` entries must resolve to a declared
    ``DomainConstraint.name`` or ``PerformanceConstraint.name``
    in the same ontology."""

    def test_covers_domain_constraint_accepted(self) -> None:
        Ontology(
            entities=[Entity(id="a", name="A")],
            domain_constraints=[_constraint("my-dc")],
            verification_cases=[
                VerificationCase(
                    name="v", covers=["my-dc"], tier="B",
                ),
            ],
        )

    def test_covers_performance_constraint_accepted(self) -> None:
        Ontology(
            entities=[Entity(id="a", name="A")],
            performance_constraints=[PerformanceConstraint(
                name="my-pc", description="", entity_ids=[],
                metric="m", budget=1.0, unit="u", direction="min",
            )],
            verification_cases=[
                VerificationCase(
                    name="v", covers=["my-pc"], tier="B",
                ),
            ],
        )

    def test_covers_dangling_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[Entity(id="a", name="A")],
                domain_constraints=[_constraint("real-one")],
                verification_cases=[VerificationCase(
                    name="v",
                    covers=["real-one", "ghost-one"],
                    tier="B",
                )],
            )
        message = str(exc.value)
        assert "ghost-one" in message
        tail = message.rsplit("ghost-one", maxsplit=1)[-1]
        assert "real-one" not in tail

    def test_covers_multi_constraint_case(self) -> None:
        Ontology(
            entities=[Entity(id="a", name="A")],
            domain_constraints=[_constraint("c1"), _constraint("c2")],
            performance_constraints=[PerformanceConstraint(
                name="p1", description="", entity_ids=[],
                metric="m", budget=1.0, unit="u", direction="min",
            )],
            verification_cases=[VerificationCase(
                name="v", covers=["c1", "c2", "p1"], tier="B",
            )],
        )

    def test_dangling_covers_joins_other_ri_errors(self) -> None:
        """RI errors from multiple sources surface together in
        one ValidationError per the established pattern."""
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[Entity(id="a", name="A")],
                verification_cases=[VerificationCase(
                    name="v", covers=["ghost-covers"], tier="B",
                )],
                relationships=[Relationship(
                    source_entity_id="ghost-source",
                    target_entity_id="a",
                    name="r", cardinality="one_to_one",
                )],
            )
        message = str(exc.value)
        assert "'ghost-covers'" in message
        assert "'ghost-source'" in message
