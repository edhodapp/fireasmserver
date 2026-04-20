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

from datetime import date as _date

from pydantic import BaseModel, ValidationError, model_validator

from ontology.types import (
    Cardinality,
    Description,
    IsoDate,
    ModuleStatus,
    PerfDirection,
    Priority,
    PropertyKind,
    RequirementStatus,
    SafeId,
    ShortName,
    SideSessionStatus,
)


# -- Problem Domain --


_SCALAR_KINDS = frozenset({"str", "int", "float", "bool", "datetime"})


def _validate_scalar_kind_reference(kind: str, reference: Any) -> None:
    """Scalar kinds (str/int/float/bool/datetime) take no reference.
    Presence of one indicates a schema mistake."""
    if reference is not None:
        raise ValueError(
            f"PropertyType kind={kind!r} does not take a reference; "
            f"got {reference!r}"
        )


def _validate_entity_ref_reference(reference: Any) -> None:
    """``kind='entity_ref'`` MUST carry a non-empty string reference
    naming the target Entity id. The RI check below resolves the
    id against the Ontology's declared entities."""
    if not isinstance(reference, str) or not reference:
        raise ValueError(
            f"PropertyType kind='entity_ref' requires a non-empty "
            f"string reference naming the target entity; got "
            f"{reference!r}"
        )


def _validate_enum_reference(reference: Any) -> None:
    """``kind='enum'`` MUST carry a non-empty list of non-empty
    strings (the allowed values). Mixed-type lists, empty lists,
    and empty-string elements are all schema mistakes."""
    if not isinstance(reference, list) or not reference:
        raise ValueError(
            f"PropertyType kind='enum' requires a non-empty list of "
            f"allowed string values; got {reference!r}"
        )
    if not all(isinstance(v, str) and v for v in reference):
        raise ValueError(
            f"PropertyType kind='enum' reference must be a list of "
            f"non-empty strings; got {reference!r}"
        )


class PropertyType(BaseModel):
    """Type descriptor for an entity property.

    The ``kind`` / ``reference`` pair has implicit rules enforced
    by ``_kind_reference_consistent`` below:

    - Scalar kinds (``str``, ``int``, ``float``, ``bool``,
      ``datetime``): ``reference`` MUST be ``None``.
    - ``entity_ref``: ``reference`` MUST be a non-empty string
      naming a target entity; the containing ``Ontology``'s RI
      check resolves the id.
    - ``enum``: ``reference`` MUST be a non-empty list of strings
      (the allowed values).
    - ``list``: ``reference`` is permissive for now — a future
      tightening can require element-type naming when a concrete
      use case arises.
    """

    kind: PropertyKind
    reference: str | list[str] | None = None

    @model_validator(mode="after")
    def _kind_reference_consistent(self) -> "PropertyType":
        if self.kind in _SCALAR_KINDS:
            _validate_scalar_kind_reference(self.kind, self.reference)
        elif self.kind == "entity_ref":
            _validate_entity_ref_reference(self.reference)
        elif self.kind == "enum":
            _validate_enum_reference(self.reference)
        return self


class Property(BaseModel):
    """A named, typed property on a domain entity."""

    name: ShortName
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
    """A domain-level invariant or business rule.

    O2 extensions give each constraint enough SysE traceability for
    an external reviewer to audit end-to-end:

    - ``rationale`` — a decision pointer (DECISIONS.md D-entry,
      requirement row like ``ETH-005``, or free-text if no formal
      origin exists). Empty string marks an orphan constraint that
      the audit tool flags.
    - ``implementation_refs`` — zero-or-more ``file:symbol`` strings
      naming the code that realizes the constraint. Empty list means
      "specification only, not yet implemented" (paired with
      ``status="spec"``).
    - ``verification_refs`` — zero-or-more pointers to the test /
      measurement / gate that proves the constraint holds. Empty
      list means "no evidence yet" and should pair with
      ``status`` of ``spec`` or ``deviation``.
    - ``status`` — requirement lifecycle position; see
      ``RequirementStatus`` docstring. Default is ``spec`` because
      a newly-authored constraint is, until proven otherwise, just
      a written-down intent.
    """

    name: str
    description: str
    entity_ids: list[str] = []
    expression: str = ""
    rationale: str = ""
    implementation_refs: list[str] = []
    verification_refs: list[str] = []
    status: RequirementStatus = "spec"


class PerformanceConstraint(BaseModel):
    """A quantitative perf budget the system must satisfy.

    Distinct from ``DomainConstraint`` because perf rows need the
    budget *number* as first-class data (not buried in description
    text) — the audit tool compares measured values against these
    budgets directly.

    - ``metric`` — short identifier the measurement harness emits
      (e.g., ``fsa_transition_ns``, ``crc32_cycles_per_byte``,
      ``obs_disabled_path_cycles``).
    - ``budget`` — numeric value the metric is compared against.
    - ``unit`` — free-text unit for human readability
      (``ns``, ``cycles``, ``bps``, ``cycles_per_byte``, ``Hz``).
    - ``direction`` — comparison direction; see ``PerfDirection``.
    - ``measured_via`` — where the measurement comes from (OSACA
      output, microbenchmark path, pre-push perf gate, etc.).
    - ``rationale``/``implementation_refs``/``verification_refs``/
      ``status`` have the same SysE-traceability semantics as on
      ``DomainConstraint``.

    A row with ``status="implemented"`` means we have both a budget
    AND a measured value that satisfies ``direction(budget)``. The
    measured value itself is NOT stored here — it belongs to the
    perf-ratchet artifact (D040) and is read by the audit tool at
    review time, not pinned into the requirements doc.
    """

    name: str
    description: str
    entity_ids: list[str] = []
    metric: str
    budget: float
    unit: str
    direction: PerfDirection
    measured_via: str = ""
    rationale: str = ""
    implementation_refs: list[str] = []
    verification_refs: list[str] = []
    status: RequirementStatus = "spec"


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
    """Specification for a Python module.

    ``dependencies`` is a mixed-domain list: entries may be
    internal module names (resolving to another ``ModuleSpec.name``
    in the same ontology), Python stdlib module names
    (``subprocess``, ``pathlib``), or third-party import paths
    (``urllib.request``). The ``_dependencies_hygiene`` validator
    catches obvious data problems — empty strings, leading /
    trailing whitespace, duplicates within a single module — but
    does NOT resolve references against the ontology, because
    there is no consistent type-tag distinguishing internal from
    external today. Splitting into a structured
    ``internal_module_refs`` field (validated against this
    ontology's module names) + a free-form ``external_imports``
    field is an open structural question — tracked in
    ``project_ontology_hygiene_gaps.md`` for a future refactor
    when the downstream tooling actually needs the distinction.
    """

    name: str
    responsibility: str
    classes: list[ClassSpec] = []
    functions: list[FunctionSpec] = []
    dependencies: list[str] = []
    test_strategy: str = ""
    status: ModuleStatus = "not_started"

    @model_validator(mode="after")
    def _dependencies_hygiene(self) -> "ModuleSpec":
        """String hygiene on ``dependencies`` entries — empty,
        whitespace-containing, or duplicate entries are schema
        mistakes regardless of whether the entry is internal or
        external. No valid Python import path contains whitespace
        in any position, so the check is "any whitespace" rather
        than "leading/trailing" — simpler rule, same coverage of
        the leading/trailing case, catches interior-whitespace
        typos too."""
        seen: set[str] = set()
        for dep in self.dependencies:
            if not dep:
                raise ValueError(
                    f"ModuleSpec {self.name!r} has empty string in "
                    "dependencies"
                )
            if any(c.isspace() for c in dep):
                raise ValueError(
                    f"ModuleSpec {self.name!r} dependency "
                    f"{dep!r} contains whitespace"
                )
            if dep in seen:
                raise ValueError(
                    f"ModuleSpec {self.name!r} has duplicate "
                    f"dependency {dep!r}"
                )
            seen.add(dep)
        return self


# -- Planning State --


class OpenQuestion(BaseModel):
    """An unresolved design question."""

    id: SafeId
    text: str
    context: str = ""
    priority: Priority = "medium"
    resolved: bool = False
    resolution: str = ""


class SideSessionTask(BaseModel):
    """A scoped task dispatched to a side session (per D052).

    First ontology-dogfooding instance for non-requirements
    content: the task spec lives here, the markdown briefing
    is a rendering of it, and the git branch + commits reference
    the task node by slug+date. Lifecycle mirrors the
    ``RequirementStatus`` pattern with a task-appropriate set
    of states (see ``SideSessionStatus``).

    Fields:
    - ``slug`` — snake_case identifier; ``SafeId`` regex rejects
      path-traversal sequences, spaces, and git-ref-illegal
      characters, closing the 2026-04-20 Gemini MEDIUM finding
      about slug injection.
    - ``date`` — dispatch date as ``YYYY-MM-DD``. Combined with
      slug it forms the (slug, date) uniqueness key the
      bootstrap duplicate-check uses.
    - ``scope_paths`` — repo-relative paths the side session
      may touch. Declarative only; enforcement is the merging
      main session's review job (possibly future audit tool).
    - ``required_reading`` — reference tags resolved by the
      briefing renderer into a reading list.
    - ``deliverables`` — one-sentence summary for the briefing
      header and at-a-glance DAG inspection.
    - ``rationale`` — optional longer justification.
    - ``parent_commit_sha`` — main's tip at dispatch time; the
      branch is cut here.
    - ``status`` — lifecycle; see ``SideSessionStatus``.
    - ``commit_shas`` — SHAs of commits made on the side branch.
    - ``merge_commit_sha`` — set by the main session on merge.

    ``branch_name`` is derived from slug+date via
    ``make_branch_name`` below rather than stored, so the name
    cannot drift out of sync with the slug+date key.
    """

    slug: SafeId
    date: IsoDate
    scope_paths: list[str] = []
    required_reading: list[str] = []
    deliverables: Description
    rationale: Description = ""
    parent_commit_sha: str = ""
    status: SideSessionStatus = "dispatched"
    commit_shas: list[str] = []
    merge_commit_sha: str = ""

    @model_validator(mode="after")
    def _date_is_real_calendar_day(self) -> "SideSessionTask":
        """``IsoDate`` only enforces the ``YYYY-MM-DD`` shape, so
        ``2026-02-30`` / ``2026-13-01`` / ``0000-01-01`` would
        slip past it. Parse with ``datetime.date.fromisoformat``
        to catch impossible calendar days and out-of-range years
        (Python's MINYEAR is 1, which conveniently rejects
        astronomical year 0 that ISO-8601 would otherwise
        allow). Raises ``ValueError`` that Pydantic surfaces as
        a ``ValidationError`` on construction."""
        try:
            _date.fromisoformat(self.date)
        except ValueError as exc:
            raise ValueError(
                f"date {self.date!r} is structurally ISO-8601 but "
                f"not a real calendar day: {exc}"
            ) from exc
        return self


def make_branch_name(slug: str, date: str) -> str:
    """Canonical branch name for a ``SideSessionTask``.

    Single source of truth so the bootstrap tool, the test
    suite, and any future listing/merge helpers all agree on
    the format. Callers pass raw ``slug`` and ``date`` — this
    function does NOT re-validate them (the ``SideSessionTask``
    model does at construction time).
    """
    return f"side/{date}_{slug}"


class Ontology(BaseModel):
    """Complete ontology snapshot.

    Enforces referential integrity across the three cross-referential
    shapes at construction time: every ``Relationship``'s source and
    target IDs, every ``DomainConstraint.entity_ids`` and
    ``PerformanceConstraint.entity_ids`` value, and every
    ``DataModel.entity_id`` must name an ``Entity`` that is declared
    in this snapshot's ``entities`` list. A dangling reference raises
    ``ValidationError`` so the builder cannot ship a structurally
    broken ontology to downstream audit tooling.
    """

    entities: list[Entity] = []
    relationships: list[Relationship] = []
    domain_constraints: list[DomainConstraint] = []
    performance_constraints: list[PerformanceConstraint] = []
    modules: list[ModuleSpec] = []
    data_models: list[DataModel] = []
    external_dependencies: list[ExternalDependency] = []
    open_questions: list[OpenQuestion] = []
    side_session_tasks: list[SideSessionTask] = []

    @model_validator(mode="after")
    def _check_referential_integrity(self) -> "Ontology":
        """Verify every cross-reference points to a declared entity.

        Runs after individual-field validation so we're guaranteed
        each referenced field is already a valid pydantic object;
        this check is purely about whether the IDs resolve.

        Surfaces a single ``ValueError`` with a complete, sorted
        list of every dangling reference rather than short-
        circuiting on the first one — an auditor reading the
        error sees the whole picture, not just the first fault.
        Each reference-type is checked by a dedicated
        ``_check_*_refs`` helper so this top-level function stays
        under the project's cyclomatic-complexity cap.
        """
        known = {entity.id for entity in self.entities}
        errors: list[str] = []
        errors.extend(_check_relationship_refs(self.relationships, known))
        errors.extend(_check_id_list_refs(
            "DomainConstraint",
            [(dc.name, dc.entity_ids) for dc in self.domain_constraints],
            known,
        ))
        errors.extend(_check_id_list_refs(
            "PerformanceConstraint",
            [(pc.name, pc.entity_ids) for pc in self.performance_constraints],
            known,
        ))
        errors.extend(_check_data_model_refs(self.data_models, known))
        errors.extend(_check_property_entity_ref_refs(self.entities, known))
        if errors:
            raise ValueError(
                "referential-integrity violations:\n  - "
                + "\n  - ".join(sorted(errors)),
            )
        return self


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


def _check_relationship_refs(
    relationships: list[Relationship],
    known: set[str],
) -> list[str]:
    """Return dangling-reference messages for Relationship source
    and target IDs. Each Relationship may contribute zero, one, or
    two messages depending on which endpoint(s) are missing."""
    errors: list[str] = []
    for rel in relationships:
        if rel.source_entity_id not in known:
            errors.append(
                f"Relationship '{rel.name}' source "
                f"'{rel.source_entity_id}' not in entities"
            )
        if rel.target_entity_id not in known:
            errors.append(
                f"Relationship '{rel.name}' target "
                f"'{rel.target_entity_id}' not in entities"
            )
    return errors


def _check_id_list_refs(
    kind: str,
    items: list[tuple[str, list[str]]],
    known: set[str],
) -> list[str]:
    """Generic reference checker for owner-types that carry a list
    of entity IDs (``DomainConstraint.entity_ids``,
    ``PerformanceConstraint.entity_ids``). ``kind`` names the
    owner type in the error message; ``items`` is a list of
    ``(owner_name, entity_ids)`` pairs."""
    errors: list[str] = []
    for owner_name, entity_ids in items:
        for eid in entity_ids:
            if eid not in known:
                errors.append(
                    f"{kind} '{owner_name}' references "
                    f"'{eid}' not in entities"
                )
    return errors


def _check_data_model_refs(
    data_models: list[DataModel],
    known: set[str],
) -> list[str]:
    """Return dangling-reference messages for DataModel.entity_id
    pointers that don't resolve in the ``known`` set."""
    errors: list[str] = []
    for dm in data_models:
        if dm.entity_id not in known:
            errors.append(
                f"DataModel for class '{dm.class_name}' references "
                f"entity '{dm.entity_id}' not in entities"
            )
    return errors


def _property_entity_ref_error(
    entity_id: str, prop: Property, known: set[str]
) -> str | None:
    """Return a dangling-reference message for a single Property
    with ``kind='entity_ref'``, or None if the property does not
    hold a dangling reference. The per-property split keeps the
    outer loop under the cyclomatic-complexity cap."""
    pt = prop.property_type
    if pt.kind != "entity_ref":
        return None
    # Defensive — the PropertyType cross-field validator
    # guarantees a non-empty string reference for kind='entity_ref',
    # but model_construct() bypasses validators and the declared
    # type is a union, so the isinstance narrowing is load-bearing
    # both for runtime safety and for mypy.
    if not isinstance(pt.reference, str):
        return None
    if pt.reference in known:
        return None
    return (
        f"Property '{entity_id}.{prop.name}' entity_ref "
        f"'{pt.reference}' not in entities"
    )


def _check_property_entity_ref_refs(
    entities: list[Entity],
    known: set[str],
) -> list[str]:
    """Return dangling-reference messages for every ``Property``
    whose ``PropertyType.kind == 'entity_ref'`` and whose
    ``reference`` id does not resolve in ``known``."""
    errors: list[str] = []
    for ent in entities:
        for prop in ent.properties:
            err = _property_entity_ref_error(ent.id, prop, known)
            if err is not None:
                errors.append(err)
    return errors


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
