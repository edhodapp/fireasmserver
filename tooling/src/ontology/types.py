"""Shared scalar / literal types for the ontology schema.

Forked from python_agent.types on 2026-04-19 (see `docs/observability.md`
companion work and the O-series commits). This fork is the bleeding-
edge implementation under fireasmserver; lessons that crystallize
here flow back to `~/python_agent` when that interface stabilizes.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import StringConstraints

# -- Constrained string types --

SafeId = Annotated[str, StringConstraints(
    pattern=r"^[a-zA-Z0-9_-]+$",
    max_length=100,
)]

ShortName = Annotated[str, StringConstraints(
    max_length=100,
)]

Description = Annotated[str, StringConstraints(
    max_length=4000,
)]

# -- Literal types for enum-like fields --

PropertyKind = Literal[
    "str", "int", "float", "bool", "datetime",
    "entity_ref", "list", "enum",
]

Cardinality = Literal[
    "one_to_one", "one_to_many",
    "many_to_one", "many_to_many",
]

ModuleStatus = Literal[
    "not_started", "in_progress", "complete",
]

Priority = Literal["low", "medium", "high"]
