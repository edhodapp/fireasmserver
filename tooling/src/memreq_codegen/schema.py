"""Pydantic models for `regions.yaml`.

Supports two YAML forms for `size` / `align`:

  literal-only (backward compatible — D066 step 5a/5b/5c/5d shape):
    size: 4096
    align: 4096

  expression (D066 task #28, this commit):
    size:
      - {kind: lit, value: 524288}
      - {kind: cpu, field: cores_sharing_l3}
      - {kind: mul}

The expression form is a postfix opcode list. Each entry is a
tagged-union dict with a `kind` discriminator. CPU/TUNING ops
reference a named field on `CpuCharacteristics` / `TuningProfile`
(see `tooling/src/memlayout/models.py`); validation resolves the
name to its positional index, which is what the wire encoding
uses.

The literal-int shortcut maps to a single-op list internally —
both forms produce identical wire bytes via the shared encoder.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal, Union

from memlayout.models import CpuCharacteristics, TuningProfile
from pydantic import BaseModel, ConfigDict, Field, field_validator

from memreq_codegen.encoding import (
    OP_ALIGN_UP,
    OP_CALL_THUNK,
    OP_CPU,
    OP_DIV_LIT,
    OP_LIT,
    OP_MUL,
    OP_TUNING,
    Op,
)

# Constrain identifier shape so a typo doesn't silently produce a
# label that collides with another symbol or that the assembler
# can't parse. Lower-case with digits and underscores; must start
# with a letter to avoid leading-digit identifiers.
_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"

# Pinned expected field order. Mirrors the declaration order in
# `tooling/src/memlayout/models.py`; the wire ABI binds CPU/TUNING
# opcode u8 ids to these positions, so a silent reorder upstream
# would mis-encode every region using these ops. The asserts below
# catch drift at codegen-import time rather than letting it surface
# as a runtime LAYOUT-INVALID under the asm interpreter.
_EXPECTED_CPU_FIELDS: tuple[str, ...] = (
    "l1d_line_bytes",
    "l1d_bytes",
    "l1i_bytes",
    "l2_bytes",
    "l3_bytes_per_cluster",
    "cores_sharing_l2",
    "cores_sharing_l3",
    "hw_prefetcher_stride_lines",
    "detected_model_id",
)
_EXPECTED_TUNING_FIELDS: tuple[str, ...] = (
    "rx_queue_depth",
    "tx_queue_depth",
    "rx_buffer_bytes_hint",
    "actor_pool_size_per_core",
    "tls_session_cache_entries",
    "worker_core_count",
)


def _check_field_order(
    label: str,
    expected: tuple[str, ...],
    actual: tuple[str, ...],
) -> None:
    if actual != expected:  # pragma: no cover
        # Defensive: reachable only via an actual upstream reorder
        # of CpuCharacteristics or TuningProfile in
        # tooling/src/memlayout/models.py. The error path is the
        # whole point of this guard — there's no value in
        # synthetically forcing it from a test.
        raise RuntimeError(
            f"{label} field order drifted from codegen's pinned "
            f"snapshot. Pinned: {expected}. Actual: {actual}. "
            f"Reordering this model is a wire-incompatible change; "
            f"update _EXPECTED_{label.upper()}_FIELDS only after "
            f"auditing every regions.yaml that uses {label} ops."
        )


_check_field_order(
    "cpu",
    _EXPECTED_CPU_FIELDS,
    tuple(CpuCharacteristics.model_fields.keys()),
)
_check_field_order(
    "tuning",
    _EXPECTED_TUNING_FIELDS,
    tuple(TuningProfile.model_fields.keys()),
)

_CPU_FIELD_IDS: dict[str, int] = {
    name: idx for idx, name in enumerate(_EXPECTED_CPU_FIELDS)
}
_TUNING_FIELD_IDS: dict[str, int] = {
    name: idx for idx, name in enumerate(_EXPECTED_TUNING_FIELDS)
}


class LitOp(BaseModel):
    """`LIT u32` — push a literal value (0..2³²-1) onto the stack."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["lit"]
    value: int = Field(ge=0, le=0xFFFFFFFF)


class CpuOp(BaseModel):
    """`CPU u8` — push `cpu_characteristics[id]`.

    `field` is the human-readable Python attribute name on
    `CpuCharacteristics`; resolved to its positional index at
    validation time. Unknown names fail with a clear error.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["cpu"]
    field: str

    @field_validator("field")
    @classmethod
    def _resolve_field(cls, value: str) -> str:
        if value not in _CPU_FIELD_IDS:
            known = ", ".join(sorted(_CPU_FIELD_IDS))
            raise ValueError(
                f"unknown CpuCharacteristics field '{value}'; "
                f"known: {known}"
            )
        return value


class TuningOp(BaseModel):
    """`TUNING u8` — push `tuning_profile[id]`."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["tuning"]
    field: str

    @field_validator("field")
    @classmethod
    def _resolve_field(cls, value: str) -> str:
        if value not in _TUNING_FIELD_IDS:
            known = ", ".join(sorted(_TUNING_FIELD_IDS))
            raise ValueError(
                f"unknown TuningProfile field '{value}'; "
                f"known: {known}"
            )
        return value


class MulOp(BaseModel):
    """`MUL` — pop b, pop a, push a*b. u64-overflow checked."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["mul"]


class DivLitOp(BaseModel):
    """`DIV_LIT u8` — pop a, push a / divisor. divisor must be > 0."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["div_lit"]
    divisor: int = Field(ge=1, le=0xFF)


class AlignUpOp(BaseModel):
    """`ALIGN_UP` — pop align, pop value, push align_up(value, align).

    align must be a power of two; checked at runtime by the
    interpreter. Codegen doesn't pre-validate because the value
    may come from a CPU/TUNING op whose result isn't known until
    phase 2.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["align_up"]


class CallThunkOp(BaseModel):
    """`CALL_THUNK u32` — call named thunk by id; push return value."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["call_thunk"]
    thunk_id: int = Field(ge=0, le=0xFFFFFFFF)


# Discriminated union of all opcode dicts. Pydantic picks the
# right model by the `kind` literal at validation time.
SizeOp = Annotated[
    Union[  # noqa: UP007 — pydantic discriminator needs `Union`
        LitOp,
        CpuOp,
        TuningOp,
        MulOp,
        DivLitOp,
        AlignUpOp,
        CallThunkOp,
    ],
    Field(discriminator="kind"),
]


# Dispatch table keyed by model type. Adding a new op kind = one
# new entry here + one new OP_* constant in encoding.py.
def _lit(m: LitOp) -> Op:
    return Op(OP_LIT, m.value)


def _cpu(m: CpuOp) -> Op:
    return Op(OP_CPU, _CPU_FIELD_IDS[m.field])


def _tuning(m: TuningOp) -> Op:
    return Op(OP_TUNING, _TUNING_FIELD_IDS[m.field])


def _mul(m: MulOp) -> Op:  # pylint: disable=unused-argument
    return Op(OP_MUL)


def _div_lit(m: DivLitOp) -> Op:
    return Op(OP_DIV_LIT, m.divisor)


def _align_up(m: AlignUpOp) -> Op:  # pylint: disable=unused-argument
    return Op(OP_ALIGN_UP)


def _call_thunk(m: CallThunkOp) -> Op:
    return Op(OP_CALL_THUNK, m.thunk_id)


_TO_OP_DISPATCH: dict[type, Callable[..., Op]] = {
    LitOp: _lit,
    CpuOp: _cpu,
    TuningOp: _tuning,
    MulOp: _mul,
    DivLitOp: _div_lit,
    AlignUpOp: _align_up,
    CallThunkOp: _call_thunk,
}


def to_op(op_model: object) -> Op:
    """Convert a validated schema op model into an encoder `Op`.

    Centralizes the schema-to-wire mapping so CPU/TUNING field-name
    resolution stays in one place. The encoder side stays unaware
    of model semantics; it only consumes typed ints. Dispatch table
    keyed by model type — adding a new op kind requires one entry
    here and the corresponding op constant in encoding.py.
    """
    handler = _TO_OP_DISPATCH.get(type(op_model))
    if handler is None:  # pragma: no cover
        # Defensive: every op model type has an entry in
        # _TO_OP_DISPATCH. Reachable only if a future commit adds
        # an op model class to the discriminated union without
        # updating the dispatch table — fail loudly rather than
        # silently emitting wrong bytes.
        raise TypeError(
            f"unknown op model {type(op_model).__name__}"
        )
    return handler(op_model)


# Size and align accept either a u32 literal (the backward-
# compatible shortcut) or a non-empty list of tagged-union ops
# (the expression form). The encoder turns either form into the
# same wire bytes.
SizeExpr = Union[  # noqa: UP007 — pydantic-compatible alias
    int,
    list[SizeOp],
]


class RegionDecl(BaseModel):
    """One region declaration in `regions.yaml`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=_NAME_PATTERN)
    tier: Literal["hot", "cold", "init"]
    lifetime: Literal[
        "steady_state",
        "init_only",
        "immutable_after_init",
        "stack",
    ]
    owner: int = Field(ge=0, le=0xFFFF)
    writable: bool
    size: SizeExpr = Field(...)
    align: SizeExpr = Field(...)
    doc: str = ""

    @field_validator("size", "align")
    @classmethod
    def _validate_size_or_align(
        cls, value: SizeExpr,
    ) -> SizeExpr:
        if isinstance(value, int):
            if not 1 <= value <= 0xFFFFFFFF:
                raise ValueError(
                    f"literal size/align {value} out of u32 "
                    f"range [1, 0xFFFFFFFF]"
                )
            return value
        if not value:
            raise ValueError(
                "empty op list (size/align must have at least "
                "one op; use the integer shortcut for literals)"
            )
        return value


class RegionFile(BaseModel):
    """Top-level shape of `regions.yaml`."""

    model_config = ConfigDict(extra="forbid")

    regions: list[RegionDecl]
