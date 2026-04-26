"""Hand-authored tests for the bump allocator.

Covers happy paths, alignment boundaries, mixed forward + reverse
passes, the stack/non-stack split, and overflow conditions.
"""

import struct

import pytest

from memlayout.bytecode import BytecodeError
from memlayout.models import (
    CpuCharacteristics,
    MemoryRegion,
    TuningProfile,
)
from memlayout.reference import LayoutOverflow, allocate
from memlayout.types import Lifetime, Opcode


def _b(*items: int | bytes) -> bytes:
    out = b""
    for item in items:
        if isinstance(item, int):
            out += bytes([item])
        else:
            out += item
    return out


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _lit_size(size: int) -> bytes:
    return _b(Opcode.LIT, _u32(size), Opcode.END)


def _lit_align(align: int) -> bytes:
    return _b(Opcode.LIT, _u32(align), Opcode.END)


@pytest.fixture(name="cpu")
def fixture_cpu() -> CpuCharacteristics:
    return CpuCharacteristics(
        l1d_line_bytes=64, l1d_bytes=32_768, l1i_bytes=32_768,
        l2_bytes=262_144, l3_bytes_per_cluster=0,
        cores_sharing_l2=1, cores_sharing_l3=1,
        hw_prefetcher_stride_lines=0, detected_model_id=0,
    )


@pytest.fixture(name="profile")
def fixture_profile() -> TuningProfile:
    return TuningProfile(
        rx_queue_depth=256, tx_queue_depth=256,
        rx_buffer_bytes_hint=2048, actor_pool_size_per_core=64,
        tls_session_cache_entries=1024, worker_core_count=4,
    )


def _region(
    name: str,
    size: int,
    align: int,
    lifetime: Lifetime = Lifetime.STEADY_STATE,
) -> MemoryRegion:
    return MemoryRegion(
        name=name,
        name_hash=hash(name) & 0xFFFFFFFF,
        size_bytecode=_lit_size(size),
        align_bytecode=_lit_align(align),
        owner_id=0,
        lifetime=lifetime,
        writable=True,
    )


def test_empty_regions_returns_empty_layout(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    layout = allocate(
        [], cpu, profile,
        heap_start=0x1000, ram_top=0x10000,
    )
    assert layout.assignments == ()
    assert layout.forward_bump_end == 0x1000
    assert layout.reverse_bump_end == 0x10000


def test_single_region_at_heap_start(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    layout = allocate(
        [_region("a", size=128, align=0x1000)],
        cpu, profile,
        heap_start=0x1000, ram_top=0x10000,
    )
    assert len(layout.assignments) == 1
    assigned = layout.assignments[0]
    assert assigned.name == "a"
    assert assigned.addr == 0x1000
    assert assigned.size == 128


def test_alignment_pads_forward_bump(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # heap starts at 0x1001 (deliberately misaligned); first region
    # needs 0x1000 alignment, so it lands at 0x2000.
    layout = allocate(
        [_region("a", size=128, align=0x1000)],
        cpu, profile,
        heap_start=0x1001, ram_top=0x10000,
    )
    assert layout.assignments[0].addr == 0x2000


def test_two_forward_regions_pack(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    layout = allocate(
        [
            _region("a", size=128, align=64),
            _region("b", size=256, align=64),
        ],
        cpu, profile,
        heap_start=0x1000, ram_top=0x10000,
    )
    assert layout.assignments[0].addr == 0x1000
    assert layout.assignments[0].size == 128
    assert layout.assignments[1].addr == 0x1080  # 0x1000 + 128
    assert layout.assignments[1].size == 256


def test_stack_region_reverse_bumps_from_ram_top(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    layout = allocate(
        [_region(
            "boot_stack", size=0x10000, align=0x1000,
            lifetime=Lifetime.STACK,
        )],
        cpu, profile,
        heap_start=0x1000, ram_top=0x100000,
    )
    assert len(layout.assignments) == 1
    # ram_top - size = 0x100000 - 0x10000 = 0xF0000
    assert layout.assignments[0].addr == 0xF0000
    assert layout.assignments[0].size == 0x10000


def test_mixed_pass_orders_forward_then_reverse(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    layout = allocate(
        [
            _region("a", size=128, align=64),
            _region(
                "stk", size=0x1000, align=0x1000,
                lifetime=Lifetime.STACK,
            ),
            _region("b", size=256, align=64),
        ],
        cpu, profile,
        heap_start=0x1000, ram_top=0x100000,
    )
    # Output is forward-pass-first then reverse.
    names = [a.name for a in layout.assignments]
    assert names == ["a", "b", "stk"]
    assert layout.assignments[0].addr == 0x1000
    assert layout.assignments[1].addr == 0x1080
    # reverse: 0x100000 - 0x1000 = 0xFF000
    assert layout.assignments[2].addr == 0xFF000


def test_forward_crosses_reverse_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # Tiny RAM; forward consumes everything; stack can't fit.
    with pytest.raises(LayoutOverflow, match="stack region"):
        allocate(
            [
                _region("hog", size=0x9000, align=0x1000),
                _region(
                    "stk", size=0x1000, align=0x1000,
                    lifetime=Lifetime.STACK,
                ),
            ],
            cpu, profile,
            heap_start=0x1000, ram_top=0xA000,
        )


def test_heap_start_above_ram_top_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    with pytest.raises(LayoutOverflow, match="heap_start"):
        allocate(
            [], cpu, profile,
            heap_start=0x10000, ram_top=0x1000,
        )


def test_assigned_size_matches_bytecode_evaluation(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # Use a non-trivial size expression: tuning.rx_queue_depth × 16
    size_bc = _b(
        Opcode.TUNING, 0,           # rx_queue_depth = 256
        Opcode.LIT, _u32(16),
        Opcode.MUL,
        Opcode.END,
    )
    region = MemoryRegion(
        name="rx_pool",
        name_hash=0xDEADBEEF,
        size_bytecode=size_bc,
        align_bytecode=_lit_align(64),
        owner_id=0,
        lifetime=Lifetime.STEADY_STATE,
        writable=True,
    )
    layout = allocate(
        [region], cpu, profile,
        heap_start=0x1000, ram_top=0x100000,
    )
    assert layout.assignments[0].size == 256 * 16


def test_immutable_after_init_uses_forward_pass(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    layout = allocate(
        [_region(
            "trust_store", size=4096, align=0x1000,
            lifetime=Lifetime.IMMUTABLE_AFTER_INIT,
        )],
        cpu, profile,
        heap_start=0x1000, ram_top=0x10000,
    )
    # Forward-pass-placed (not at top-of-RAM).
    assert layout.assignments[0].addr == 0x1000


def test_init_only_uses_forward_pass(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    layout = allocate(
        [_region(
            "boot_scratch", size=128, align=64,
            lifetime=Lifetime.INIT_ONLY,
        )],
        cpu, profile,
        heap_start=0x1000, ram_top=0x10000,
    )
    assert layout.assignments[0].addr == 0x1000


def test_thunk_used_for_size(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    size_bc = _b(Opcode.CALL_THUNK, _u32(7), Opcode.END)
    region = MemoryRegion(
        name="exotic",
        name_hash=0,
        size_bytecode=size_bc,
        align_bytecode=_lit_align(64),
        owner_id=0,
        lifetime=Lifetime.STEADY_STATE,
        writable=True,
    )
    thunks = {7: lambda _c, _p: 0x1234}
    layout = allocate(
        [region], cpu, profile,
        heap_start=0x1000, ram_top=0x100000,
        thunks=thunks,
    )
    assert layout.assignments[0].size == 0x1234


def test_align_bytecode_returning_non_pow2_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # align expression evaluates to 3, which is not a power of two.
    region = MemoryRegion(
        name="bad",
        name_hash=0,
        size_bytecode=_lit_size(64),
        align_bytecode=_b(Opcode.LIT, _u32(3), Opcode.END),
        owner_id=0,
        lifetime=Lifetime.STEADY_STATE,
        writable=True,
    )
    with pytest.raises(LayoutOverflow, match="power of two"):
        allocate(
            [region], cpu, profile,
            heap_start=0x1000, ram_top=0x10000,
        )


def test_align_bytecode_zero_align_for_stack_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # Same check fires in the reverse pass via _align_down.
    region = MemoryRegion(
        name="badstk",
        name_hash=0,
        size_bytecode=_lit_size(64),
        align_bytecode=_b(Opcode.LIT, _u32(0), Opcode.END),
        owner_id=0,
        lifetime=Lifetime.STACK,
        writable=True,
    )
    # Zero-align is rejected by the bytecode interpreter (ALIGN_UP
    # check). This still surfaces as a halt; either flavor is OK
    # for the asm side, both are LAYOUT-INVALID equivalents.
    with pytest.raises((BytecodeError, LayoutOverflow)):
        allocate(
            [region], cpu, profile,
            heap_start=0x1000, ram_top=0x10000,
        )


def test_huge_size_overflows_forward_bump(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # Size is the maximum u32 (4 GiB - 1); allocator's forward
    # bump from heap_start crosses 2^64 - reachable because
    # heap_start can be near the top of the address space.
    huge = MemoryRegion(
        name="huge",
        name_hash=0,
        size_bytecode=_b(
            Opcode.LIT, _u32(0xFFFFFFFF), Opcode.END,
        ),
        align_bytecode=_lit_align(0x1000),
        owner_id=0,
        lifetime=Lifetime.STEADY_STATE,
        writable=True,
    )
    with pytest.raises(LayoutOverflow, match="overflow"):
        allocate(
            [huge], cpu, profile,
            heap_start=0xFFFFFFFFFFFF0000,
            ram_top=0xFFFFFFFFFFFFFFFF,
        )


def test_stack_size_exceeds_ram_top_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # Reverse bump's `size > bump` underflow path.
    too_big = MemoryRegion(
        name="huge_stk",
        name_hash=0,
        size_bytecode=_b(
            Opcode.LIT, _u32(0xFFFFFFF0), Opcode.END,
        ),
        align_bytecode=_lit_align(0x1000),
        owner_id=0,
        lifetime=Lifetime.STACK,
        writable=True,
    )
    with pytest.raises(LayoutOverflow, match="underflow"):
        allocate(
            [too_big], cpu, profile,
            heap_start=0x1000, ram_top=0x10000,
        )


def test_disjointness_holds(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    layout = allocate(
        [
            _region("a", size=100, align=64),
            _region("b", size=200, align=64),
            _region("c", size=300, align=64),
            _region(
                "stk", size=0x1000, align=0x1000,
                lifetime=Lifetime.STACK,
            ),
        ],
        cpu, profile,
        heap_start=0x1000, ram_top=0x100000,
    )
    ranges = [
        (a.addr, a.addr + a.size)
        for a in layout.assignments
    ]
    for i, (lo_i, hi_i) in enumerate(ranges):
        for j, (lo_j, hi_j) in enumerate(ranges):
            if i == j:
                continue
            assert hi_i <= lo_j or hi_j <= lo_i, (
                f"overlap between {i} and {j}: "
                f"[{lo_i}, {hi_i}) vs [{lo_j}, {hi_j})"
            )
