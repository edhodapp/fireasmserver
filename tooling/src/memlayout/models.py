"""Pydantic models for the memlayout reference.

These models are the test surface and the audit-input surface.
They mirror but do NOT serialize to the wire-level .memreq
record layout — that mapping lives in record_codec (added in
step 3 alongside the assembly interpreter).
"""

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from memlayout.types import (
    ALIGN_BYTECODE_BYTES,
    Lifetime,
    MAX_U64,
    SIZE_BYTECODE_BYTES,
)

OWNER_BOOT_CORE = 0
OWNER_SHARED_RO = 0xFFFF


class CpuCharacteristics(BaseModel):
    """Layer-1 CPU intrinsics, populated by phase 0 detection.

    Field IDs are positional: index in this struct matches the
    `CPU <id>` opcode payload byte. Reordering is binary-
    incompatible with the assembly interpreter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    l1d_line_bytes: int = Field(ge=1, le=MAX_U64)
    l1d_bytes: int = Field(ge=1, le=MAX_U64)
    l1i_bytes: int = Field(ge=1, le=MAX_U64)
    l2_bytes: int = Field(ge=1, le=MAX_U64)
    l3_bytes_per_cluster: int = Field(ge=0, le=MAX_U64)
    cores_sharing_l2: int = Field(ge=1, le=255)
    cores_sharing_l3: int = Field(ge=1, le=255)
    hw_prefetcher_stride_lines: int = Field(ge=0, le=255)
    detected_model_id: int = Field(ge=0, le=MAX_U64)


class TuningProfile(BaseModel):
    """Layer-2 deployment-tuning parameters.

    Like CpuCharacteristics, field IDs are positional.
    Field-by-field valid_range enforcement happens in
    reference.allocate before the bytecode runs (PROFILE-INVALID
    halt path).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rx_queue_depth: int = Field(ge=1, le=MAX_U64)
    tx_queue_depth: int = Field(ge=1, le=MAX_U64)
    rx_buffer_bytes_hint: int = Field(ge=1, le=MAX_U64)
    actor_pool_size_per_core: int = Field(ge=0, le=MAX_U64)
    tls_session_cache_entries: int = Field(ge=0, le=MAX_U64)
    worker_core_count: int = Field(ge=1, le=255)


class MemoryRegion(BaseModel):
    """One .memreq declaration. Mirrors the on-the-wire record.

    size_bytecode and align_bytecode are the same byte sequences
    the assembly macro emits — END-terminated opcode streams
    that the bytecode VM evaluates against the frozen Layer 1 +
    Layer 2 tables.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    name_hash: int = Field(ge=0, le=0xFFFFFFFF)
    size_bytecode: bytes = Field(max_length=SIZE_BYTECODE_BYTES)
    align_bytecode: bytes = Field(max_length=ALIGN_BYTECODE_BYTES)
    owner_id: int = Field(ge=0, le=0xFFFF)
    lifetime: Lifetime
    writable: bool


class AssignedRegion(BaseModel):
    """Allocator output for one MemoryRegion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    addr: int = Field(ge=0, le=MAX_U64)
    size: int = Field(ge=0, le=MAX_U64)


class Layout(BaseModel):
    """Allocator output for an entire run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assignments: tuple[AssignedRegion, ...]
    forward_bump_end: int = Field(ge=0, le=MAX_U64)
    reverse_bump_end: int = Field(ge=0, le=MAX_U64)


# A thunk is invoked by the CALL_THUNK opcode. Test harnesses
# register synthetic thunks against integer ids; on the asm side
# the same id resolves to a named function via the linker.
ThunkFn = Callable[[CpuCharacteristics, TuningProfile], int]
