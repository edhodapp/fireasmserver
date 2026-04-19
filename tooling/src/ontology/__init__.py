"""fireasmserver ontology — bleeding-edge fork of python_agent.ontology.

Forked 2026-04-19 to let SysE-grade schema extensions (rationale,
implementation_refs, verification_refs, status, PerformanceConstraint)
land in this project without coordination through the python_agent
session. Lessons flow back to python_agent after first release.
"""

from ontology.models import (
    ClassSpec,
    DAGEdge,
    DAGNode,
    DataModel,
    Decision,
    DomainConstraint,
    Entity,
    ExternalDependency,
    FunctionSpec,
    ModuleSpec,
    Ontology,
    OntologyDAG,
    OpenQuestion,
    Property,
    PropertyType,
    Relationship,
    validate_ontology_strict,
)

__all__ = [
    "ClassSpec",
    "DAGEdge",
    "DAGNode",
    "DataModel",
    "Decision",
    "DomainConstraint",
    "Entity",
    "ExternalDependency",
    "FunctionSpec",
    "ModuleSpec",
    "Ontology",
    "OntologyDAG",
    "OpenQuestion",
    "Property",
    "PropertyType",
    "Relationship",
    "validate_ontology_strict",
]
