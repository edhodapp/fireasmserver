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

# Status legend for a SysE-style requirement:
#   spec        — written down, not yet implemented or verified
#   tested      — implementation plus at least one verification
#                 method, but not every derived requirement closed
#   implemented — full coverage; the system demonstrably satisfies
#                 the constraint under its stated verification
#   deviation   — the system does NOT satisfy the constraint as
#                 stated; the rationale field explains why, and the
#                 audit tool is expected to flag this row for human
#                 review even though it's "tracked"
#   n_a         — not applicable to the current platform profile /
#                 configuration; retained in the ontology for
#                 traceability against the originating decision
RequirementStatus = Literal[
    "spec", "tested", "implemented", "deviation", "n_a",
]

# Direction of a PerformanceConstraint's budget comparison.
#   max   — measured value MUST be ≤ budget (latency, cycle count)
#   min   — measured value MUST be ≥ budget (throughput, bandwidth)
#   equal — measured value MUST equal budget exactly (rare; used for
#           protocol-mandated constants like polynomial or magic)
PerfDirection = Literal["max", "min", "equal"]
