"""Shared scalar / literal types for the ontology schema.

Forked from python_agent.types on 2026-04-19 (see `docs/observability.md`
companion work and the O-series commits). This fork is the bleeding-
edge implementation under fireasmserver; lessons that crystallize
here flow back to `~/python_agent` when that interface stabilizes.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime as _datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, StringConstraints

# -- Constrained string types --

# Module-level constant so downstream code (e.g., the
# ``OntologyDAG.current_node_id`` model_validator in models.py,
# which admits the empty string as a sentinel and can't use the
# plain ``SafeId`` annotation) can re-use the exact same pattern
# without copy-pasting the literal. Single source of truth.
SAFE_ID_PATTERN = r"^[a-zA-Z0-9_][a-zA-Z0-9_-]*$"
SAFE_ID_MAX_LENGTH = 100

SafeId = Annotated[str, StringConstraints(
    # Must start with alphanumeric or underscore — forbidding a
    # leading dash closes the "shell positional-arg eats `--slug`"
    # failure mode where an id like `-rf` could be mistaken for a
    # flag by downstream tooling. Interior dashes are fine.
    pattern=SAFE_ID_PATTERN,
    max_length=SAFE_ID_MAX_LENGTH,
)]

ShortName = Annotated[str, StringConstraints(
    max_length=100,
)]

Description = Annotated[str, StringConstraints(
    max_length=4000,
)]


def _parse_iso_date(value: str) -> str:
    """Verify ``value`` is a real calendar day. The regex on
    ``IsoDate`` already enforced ``YYYY-MM-DD`` structure, so
    this step catches impossible calendar dates like
    ``2026-02-30`` / ``2026-13-01`` and year 0000 (Python's
    ``date`` MINYEAR is 1). Returns the value unchanged on
    success; raises ``ValueError`` otherwise."""
    _date.fromisoformat(value)
    return value


def _parse_iso_timestamp(value: str) -> str:
    """Verify ``value`` is a parseable ISO-8601 timestamp.
    Same pattern as ``_parse_iso_date``: regex catches shape
    mistakes, this layer catches impossible-but-structural
    values."""
    _datetime.fromisoformat(value)
    return value


# ISO-8601 date, "YYYY-MM-DD" exactly. Two layers of validation:
# (1) ``StringConstraints`` regex enforces the literal shape —
#     no two-digit years, no missing zero-padding, no whitespace.
# (2) ``AfterValidator`` calls ``date.fromisoformat`` to reject
#     impossible calendar days (Feb 30, month 13, non-leap
#     Feb-29, ISO astronomical year 0).
# Stays a string for lossless round-trip through JSON; callers
# convert to ``datetime.date`` locally if arithmetic matters.
IsoDate = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$"),
    AfterValidator(_parse_iso_date),
]

# ISO-8601 timestamp: "YYYY-MM-DDTHH:MM:SS[.ffffff][±HH:MM|Z]".
# Same two-layer pattern as ``IsoDate``. Used by ``DAGNode.created_at``
# and ``DAGEdge.created_at`` so those fields match the stricter
# bar already set for ``SideSessionTask.date``.
IsoTimestamp = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^\d{4}-\d{2}-\d{2}"                      # date
            r"T\d{2}:\d{2}:\d{2}"                      # time
            r"(\.\d+)?"                                # optional fractional
            r"([+-]\d{2}:\d{2}|Z)?$"                   # optional tz
        ),
    ),
    AfterValidator(_parse_iso_timestamp),
]

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

# Lifecycle of a dispatched side-session task. Transitions are
# NOT enforced at the Pydantic level — the model records the
# current state; the bootstrap tool and subsequent status-change
# subcommands drive the state machine (D052).
#   dispatched  — bootstrap has cut the branch and written the
#                 node; the side session has not yet started
#                 (or has not yet signaled it has started).
#   in_progress — the side session is actively committing on its
#                 branch.
#   merged      — the side branch has been merged into main; the
#                 merge commit's SHA is recorded in
#                 ``merge_commit_sha``.
#   reverted    — dispatch was abandoned; the branch and
#                 worktree have been cleaned up without merge.
SideSessionStatus = Literal[
    "dispatched", "in_progress", "merged", "reverted",
]

# Tier classification for a ``VerificationCase`` — mirrors the
# ``docs/l2/TEST_PLAN.md`` §0 four-tier harness architecture.
# The tier determines WHERE in the CD flow the test runs:
#   A — host-side unit tests (no VMM, no DMA, milliseconds)
#   B — QEMU integration tests (full guest + tap-device frame
#       injection)
#   C — adversarial / fuzz tests (broad coverage over parser +
#       virtqueue surfaces)
#   D — interop tests (cross-platform regression against other
#       stacks / switches)
TestTier = Literal["A", "B", "C", "D"]

# Lifecycle of a ``VerificationCase`` — a SysE-traceability
# record for an individual named test. Status flips in the
# same commit that transitions the test (same-commit
# convention per TEST_PLAN.md §9, so the DAG snapshot and the
# test-file state stay consistent).
#   planned    — declared but not written
#   written    — test code exists but is not yet green
#   passing    — test code exists and currently passes in CI
#   superseded — replaced by another test or by a structural
#                change that made it irrelevant; kept for the
#                audit trail, expected to carry a rationale
TestCaseStatus = Literal[
    "planned", "written", "passing", "superseded",
]
