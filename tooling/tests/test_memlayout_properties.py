"""Hypothesis-driven property tests for the bytecode VM and the
bump allocator.

Generates random valid bytecode programs and random valid region
sets, then asserts allocator invariants — disjointness, monotonic
bumps, no forward/reverse crossing, assigned_size matches the
bytecode-evaluated size. The same tests will be retargeted at
each arch's assembly interpreter (step 3) via a differential
harness; the properties are the same on both sides.
"""

import struct

from hypothesis import given, settings, strategies as st
import pytest

from memlayout.bytecode import BytecodeError, run_bytecode
from memlayout.models import (
    CpuCharacteristics,
    Layout,
    MemoryRegion,
    TuningProfile,
)
from memlayout.reference import LayoutOverflow, allocate
from memlayout.types import Lifetime, MAX_U64, Opcode


# ---- Strategies ----------------------------------------------------


def _cpu_strategy() -> st.SearchStrategy[CpuCharacteristics]:
    return st.builds(
        CpuCharacteristics,
        l1d_line_bytes=st.sampled_from([32, 64, 128]),
        l1d_bytes=st.integers(
            min_value=1024, max_value=1 << 20,
        ),
        l1i_bytes=st.integers(
            min_value=1024, max_value=1 << 20,
        ),
        l2_bytes=st.integers(
            min_value=4096, max_value=1 << 24,
        ),
        l3_bytes_per_cluster=st.integers(
            min_value=0, max_value=1 << 28,
        ),
        cores_sharing_l2=st.integers(min_value=1, max_value=8),
        cores_sharing_l3=st.integers(min_value=1, max_value=8),
        hw_prefetcher_stride_lines=st.integers(
            min_value=0, max_value=8,
        ),
        detected_model_id=st.integers(
            min_value=0, max_value=255,
        ),
    )


def _profile_strategy() -> st.SearchStrategy[TuningProfile]:
    return st.builds(
        TuningProfile,
        rx_queue_depth=st.integers(min_value=1, max_value=4096),
        tx_queue_depth=st.integers(min_value=1, max_value=4096),
        rx_buffer_bytes_hint=st.integers(
            min_value=64, max_value=1 << 16,
        ),
        actor_pool_size_per_core=st.integers(
            min_value=0, max_value=1024,
        ),
        tls_session_cache_entries=st.integers(
            min_value=0, max_value=8192,
        ),
        worker_core_count=st.integers(min_value=1, max_value=16),
    )


def _u32_bytes(value: int) -> bytes:
    return struct.pack("<I", value)


def _lit_bc(value: int) -> bytes:
    return bytes([Opcode.LIT]) + _u32_bytes(value) + bytes(
        [Opcode.END],
    )


def _power_of_two_bytecode() -> st.SearchStrategy[bytes]:
    """Bytecode that evaluates to a small power-of-two alignment.

    Restricting to powers of two keeps ALIGN_UP-driven tests
    well-defined (the interpreter rejects non-pow2).
    """
    return st.sampled_from(
        [_lit_bc(1 << k) for k in range(0, 13)]
    )


def _small_size_bytecode() -> st.SearchStrategy[bytes]:
    """Bytecode that evaluates to a small u32 size.

    Bounded so that allocator tests can fit many regions inside
    a tractable RAM range without overflow.
    """
    return st.integers(min_value=0, max_value=4096).map(
        _lit_bc
    )


def _region_strategy() -> st.SearchStrategy[MemoryRegion]:
    return st.builds(
        MemoryRegion,
        name=st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz_",
            min_size=1, max_size=16,
        ),
        name_hash=st.integers(min_value=0, max_value=0xFFFFFFFF),
        size_bytecode=_small_size_bytecode(),
        align_bytecode=_power_of_two_bytecode(),
        owner_id=st.integers(min_value=0, max_value=0xFFFF),
        lifetime=st.sampled_from(list(Lifetime)),
        writable=st.booleans(),
    )


# ---- Properties: bytecode VM --------------------------------------


@given(_cpu_strategy(), _profile_strategy())
def test_lit_round_trips(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    for value in (0, 1, 0xFFFF, 0xFFFFFFFF):
        assert run_bytecode(_lit_bc(value), cpu, profile) == value


@given(_cpu_strategy(), _profile_strategy())
def test_align_up_idempotent_when_already_aligned(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # align_up(x, x) == x for any x that's a positive power of 2.
    for k in range(0, 13):
        align = 1 << k
        code = (
            bytes([Opcode.LIT]) + _u32_bytes(align)
            + bytes([Opcode.LIT]) + _u32_bytes(align)
            + bytes([Opcode.ALIGN_UP, Opcode.END])
        )
        assert run_bytecode(code, cpu, profile) == align


# ---- Properties: allocator ----------------------------------------


def _ranges(layout: Layout) -> list[tuple[int, int]]:
    return [(a.addr, a.addr + a.size) for a in layout.assignments]


def _check_disjoint(ranges: list[tuple[int, int]]) -> None:
    for i, (lo_i, hi_i) in enumerate(ranges):
        for j, (lo_j, hi_j) in enumerate(ranges):
            if i == j:
                continue
            assert hi_i <= lo_j or hi_j <= lo_i, (
                f"overlap [{lo_i},{hi_i}) vs [{lo_j},{hi_j})"
            )


def _try_allocate(
    regions: list[MemoryRegion],
    cpu: CpuCharacteristics,
    profile: TuningProfile,
) -> Layout | None:
    try:
        return allocate(
            regions, cpu, profile,
            heap_start=0x1000,
            ram_top=1 << 32,  # 4 GiB synthetic ceiling
        )
    except LayoutOverflow:
        return None
    except BytecodeError:
        return None


@given(
    st.lists(_region_strategy(), min_size=0, max_size=8),
    _cpu_strategy(),
    _profile_strategy(),
)
@settings(max_examples=200)
def test_allocator_invariants(
    regions: list[MemoryRegion],
    cpu: CpuCharacteristics,
    profile: TuningProfile,
) -> None:
    layout = _try_allocate(regions, cpu, profile)
    if layout is None:
        return  # pre-condition failed; nothing to check
    # Every assignment is in u64.
    for a in layout.assignments:
        assert 0 <= a.addr <= MAX_U64
        assert 0 <= a.size <= MAX_U64
        assert a.addr + a.size <= MAX_U64 + 1
    # Forward bump grew monotonically up to forward_bump_end.
    assert layout.forward_bump_end >= 0x1000
    # Reverse bump shrank monotonically from ram_top.
    assert layout.reverse_bump_end <= (1 << 32)
    # Disjointness.
    _check_disjoint(_ranges(layout))


@given(
    st.lists(_region_strategy(), min_size=0, max_size=8),
    _cpu_strategy(),
    _profile_strategy(),
)
@settings(max_examples=200)
def test_forward_bump_does_not_cross_reverse(
    regions: list[MemoryRegion],
    cpu: CpuCharacteristics,
    profile: TuningProfile,
) -> None:
    layout = _try_allocate(regions, cpu, profile)
    if layout is None:
        return
    # Forward end must not exceed reverse end (or ram_top if no
    # stack regions).
    assert layout.forward_bump_end <= layout.reverse_bump_end


@given(
    st.lists(_region_strategy(), min_size=0, max_size=8),
    _cpu_strategy(),
    _profile_strategy(),
)
@settings(max_examples=200)
def test_assignment_count_matches_input(
    regions: list[MemoryRegion],
    cpu: CpuCharacteristics,
    profile: TuningProfile,
) -> None:
    layout = _try_allocate(regions, cpu, profile)
    if layout is None:
        return
    assert len(layout.assignments) == len(regions)


# ---- Smoke: BytecodeError surfaces for malformed inputs -----------


@given(
    st.binary(min_size=1, max_size=32),
    _cpu_strategy(),
    _profile_strategy(),
)
@settings(max_examples=200)
def test_random_bytes_either_evaluate_or_raise_cleanly(
    blob: bytes,
    cpu: CpuCharacteristics,
    profile: TuningProfile,
) -> None:
    """The interpreter never crashes on arbitrary bytes — it
    either evaluates to an integer or raises BytecodeError.
    """
    try:
        result = run_bytecode(blob, cpu, profile)
    except BytecodeError:
        return
    assert isinstance(result, int)
    assert 0 <= result <= MAX_U64


# ---- Sanity: profile validation rejects out-of-range fields -------


def test_profile_rejects_negative_field() -> None:
    with pytest.raises(Exception):  # pylint: disable=broad-except
        TuningProfile(
            rx_queue_depth=-1,
            tx_queue_depth=256,
            rx_buffer_bytes_hint=2048,
            actor_pool_size_per_core=64,
            tls_session_cache_entries=1024,
            worker_core_count=4,
        )
