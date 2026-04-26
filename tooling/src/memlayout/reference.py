"""Bump-allocator reference implementation (D059 + D060 phase 2).

Two-pass over the .memreq table:
  Pass 1 — non-stack lifetimes get forward-bumped from
           heap_start.
  Pass 2 — stack lifetimes get reverse-bumped from ram_top.

If the two bumps cross, raise LayoutOverflow (asm halt code:
LAYOUT-OVERFLOW). Either pass may raise BytecodeError from
its size or alignment expression; the caller surfaces that
as a LAYOUT-INVALID halt.
"""

from collections.abc import Mapping, Sequence

from memlayout.bytecode import run_bytecode
from memlayout.models import (
    AssignedRegion,
    CpuCharacteristics,
    Layout,
    MemoryRegion,
    ThunkFn,
    TuningProfile,
)
from memlayout.types import Lifetime, MAX_U64


class LayoutOverflow(Exception):
    """Raised when forward-bump crosses reverse-bump.

    Equivalent to the LAYOUT-OVERFLOW halt code on the asm side.
    """


def _eval_size_align(
    region: MemoryRegion,
    cpu: CpuCharacteristics,
    profile: TuningProfile,
    thunks: Mapping[int, ThunkFn],
) -> tuple[int, int]:
    size = run_bytecode(
        region.size_bytecode, cpu, profile, thunks,
    )
    align = run_bytecode(
        region.align_bytecode, cpu, profile, thunks,
    )
    return size, align


def _align_up(value: int, align: int) -> int:
    if align == 0 or (align & (align - 1)) != 0:
        raise LayoutOverflow(
            f"alignment {align} is not a positive power of two"
        )
    return (value + align - 1) & ~(align - 1)


def _align_down(value: int, align: int) -> int:
    if align == 0 or (align & (align - 1)) != 0:
        raise LayoutOverflow(
            f"alignment {align} is not a positive power of two"
        )
    return value & ~(align - 1)


def _forward_bump(
    bump: int, size: int, align: int,
) -> tuple[int, int]:
    addr = _align_up(bump, align)
    new_bump = addr + size
    if new_bump > MAX_U64:
        raise LayoutOverflow("forward bump overflow u64")
    return addr, new_bump


def _reverse_bump(
    bump: int, size: int, align: int,
) -> tuple[int, int]:
    if size > bump:
        raise LayoutOverflow("reverse bump underflow")
    candidate = bump - size
    addr = _align_down(candidate, align)
    return addr, addr


def _pass_forward(
    regions: Sequence[MemoryRegion],
    cpu: CpuCharacteristics,
    profile: TuningProfile,
    thunks: Mapping[int, ThunkFn],
    heap_start: int,
) -> tuple[list[AssignedRegion], int]:
    bump = heap_start
    out: list[AssignedRegion] = []
    for region in regions:
        if region.lifetime is Lifetime.STACK:
            continue
        size, align = _eval_size_align(
            region, cpu, profile, thunks,
        )
        addr, bump = _forward_bump(bump, size, align)
        out.append(AssignedRegion(
            name=region.name, addr=addr, size=size,
        ))
    return out, bump


def _pass_reverse(
    regions: Sequence[MemoryRegion],
    cpu: CpuCharacteristics,
    profile: TuningProfile,
    thunks: Mapping[int, ThunkFn],
    ram_top: int,
    forward_end: int,
) -> tuple[list[AssignedRegion], int]:
    bump = ram_top
    out: list[AssignedRegion] = []
    for region in regions:
        if region.lifetime is not Lifetime.STACK:
            continue
        size, align = _eval_size_align(
            region, cpu, profile, thunks,
        )
        addr, bump = _reverse_bump(bump, size, align)
        if addr < forward_end:
            raise LayoutOverflow(
                f"stack region {region.name} crosses "
                f"forward bump ({addr} < {forward_end})"
            )
        out.append(AssignedRegion(
            name=region.name, addr=addr, size=size,
        ))
    return out, bump


def allocate(
    regions: Sequence[MemoryRegion],
    cpu: CpuCharacteristics,
    profile: TuningProfile,
    *,
    heap_start: int,
    ram_top: int,
    thunks: Mapping[int, ThunkFn] | None = None,
) -> Layout:
    """Run the bump allocator over (regions, cpu, profile).

    Returns a Layout whose `assignments` preserve the order
    forward-pass-first, reverse-pass-second. Either pass may
    raise LayoutOverflow; bytecode evaluation may raise
    BytecodeError. Both are non-recoverable in practice (asm
    side halts the boot).
    """
    if heap_start > ram_top:
        raise LayoutOverflow(
            f"heap_start {heap_start} > ram_top {ram_top}"
        )
    used_thunks: Mapping[int, ThunkFn] = thunks or {}
    forward, forward_end = _pass_forward(
        regions, cpu, profile, used_thunks, heap_start,
    )
    reverse, reverse_end = _pass_reverse(
        regions, cpu, profile, used_thunks, ram_top, forward_end,
    )
    return Layout(
        assignments=tuple(forward + reverse),
        forward_bump_end=forward_end,
        reverse_bump_end=reverse_end,
    )
