"""Ontology data model — entities, relationships, constraints, modules, DAG.

Forked from python_agent.ontology on 2026-04-19. This commit (O1)
reproduces the upstream shape 1:1 so the existing
tooling/qemu-harness.json round-trips losslessly; subsequent commits
(O2+) grow SysE-grade fields (rationale, implementation_refs,
verification_refs, status, PerformanceConstraint) without touching
the upstream repo.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from ontology.types import (
    Cardinality,
    Description,
    ModuleStatus,
    Priority,
    PropertyKind,
    SafeId,
    ShortName,
)


# -- Problem Domain --


class PropertyType(BaseModel):
    """Type descriptor for an entity property."""

    kind: PropertyKind
    reference: str | list[str] | None = None


class Property(BaseModel):
    """A named, typed property on a domain entity."""

    name: str
    property_type: PropertyType
    description: str = ""
    required: bool = True
    constraints: list[str] = []


class Entity(BaseModel):
    """A business concept in the problem domain."""

    id: SafeId
    name: ShortName
    description: Description = ""
    properties: list[Property] = []


class Relationship(BaseModel):
    """A directed relationship between two entities."""

    source_entity_id: str
    target_entity_id: str
    name: str
    cardinality: Cardinality
    description: str = ""


class DomainConstraint(BaseModel):
    """A domain-level invariant or business rule."""

    name: str
    description: str
    entity_ids: list[str] = []
    expression: str = ""


# -- Solution Domain --


class FunctionSpec(BaseModel):
    """Specification for a function to be implemented."""

    name: str
    parameters: list[tuple[str, str]] = []
    return_type: str
    docstring: str = ""
    preconditions: list[str] = []
    postconditions: list[str] = []


class ClassSpec(BaseModel):
    """Specification for a class to be implemented."""

    name: str
    description: str = ""
    bases: list[str] = []
    methods: list[FunctionSpec] = []


class DataModel(BaseModel):
    """Maps a problem-domain entity to a code construct."""

    entity_id: str
    storage: str
    class_name: str
    notes: str = ""


class ExternalDependency(BaseModel):
    """An external package dependency."""

    name: str
    version_constraint: str = ""
    reason: str = ""


class ModuleSpec(BaseModel):
    """Specification for a Python module."""

    name: str
    responsibility: str
    classes: list[ClassSpec] = []
    functions: list[FunctionSpec] = []
    dependencies: list[str] = []
    test_strategy: str = ""
    status: ModuleStatus = "not_started"


# -- Planning State --


class OpenQuestion(BaseModel):
    """An unresolved design question."""

    id: SafeId
    text: str
    context: str = ""
    priority: Priority = "medium"
    resolved: bool = False
    resolution: str = ""


class Ontology(BaseModel):
    """Complete ontology snapshot."""

    entities: list[Entity] = []
    relationships: list[Relationship] = []
    domain_constraints: list[DomainConstraint] = []
    modules: list[ModuleSpec] = []
    data_models: list[DataModel] = []
    external_dependencies: list[ExternalDependency] = []
    open_questions: list[OpenQuestion] = []


# -- DAG Structure --


class Decision(BaseModel):
    """Records a design decision."""

    question: str
    options: list[str]
    chosen: str
    rationale: str


class DAGEdge(BaseModel):
    """An edge in the version DAG."""

    parent_id: str
    child_id: str
    decision: Decision
    created_at: str


class DAGNode(BaseModel):
    """A node in the version DAG."""

    id: str
    ontology: Ontology
    created_at: str
    label: str = ""
    integrity_hash: str = ""


class OntologyDAG(BaseModel):
    """Versioned ontology DAG."""

    project_name: str
    nodes: list[DAGNode] = []
    edges: list[DAGEdge] = []
    current_node_id: str = ""

    # -- Navigation --

    def get_node(self, node_id: str) -> DAGNode | None:
        """Find a node by ID."""
        return next(
            (n for n in self.nodes if n.id == node_id),
            None,
        )

    def get_current_node(self) -> DAGNode | None:
        """Return the currently active node."""
        return self.get_node(self.current_node_id)

    def children_of(self, node_id: str) -> list[DAGNode]:
        """Return all child nodes of the given node."""
        child_ids = {
            e.child_id
            for e in self.edges
            if e.parent_id == node_id
        }
        return [
            n for n in self.nodes if n.id in child_ids
        ]

    def parents_of(self, node_id: str) -> list[DAGNode]:
        """Return all parent nodes of the given node."""
        parent_ids = {
            e.parent_id
            for e in self.edges
            if e.child_id == node_id
        }
        return [
            n for n in self.nodes if n.id in parent_ids
        ]

    def root_nodes(self) -> list[DAGNode]:
        """Return all nodes with no parents."""
        child_ids = {e.child_id for e in self.edges}
        return [
            n for n in self.nodes
            if n.id not in child_ids
        ]

    def edges_from(self, node_id: str) -> list[DAGEdge]:
        """Return all edges from the given node."""
        return [
            e for e in self.edges
            if e.parent_id == node_id
        ]

    def edges_to(self, node_id: str) -> list[DAGEdge]:
        """Return all edges to the given node."""
        return [
            e for e in self.edges
            if e.child_id == node_id
        ]

    # -- Serialization --

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, text: str) -> "OntologyDAG":
        """Deserialize from JSON string."""
        return cls.model_validate_json(text)


# -- Validation --


def validate_ontology_strict(
    data: dict[str, Any],
) -> list[str]:
    """Validate ontology data from external input.

    Returns list of error strings, empty if valid.
    """
    try:
        Ontology.model_validate(data)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(x) for x in e['loc'])}: "
            f"{e['msg']}"
            for e in exc.errors()
        ]
    return []
