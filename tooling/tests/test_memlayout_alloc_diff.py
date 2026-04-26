"""Differential tests: per-arch asm bump allocator vs Python ref.

For each arch, a hand-authored set of region tables runs through
both implementations; the (rc, forward_end, reverse_end,
assignments) tuples must agree.
"""

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from memlayout.diffharness import (
    alloc_driver_path,
    python_alloc_verdict,
    run_asm_alloc,
)
from memlayout.models import (
    CpuCharacteristics,
    MemoryRegion,
    TuningProfile,
)
from memlayout.types import Lifetime, Opcode

ARCHES = ("x86_64", "aarch64")


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _b(*items: int | bytes) -> bytes:
    out = b""
    for item in items:
        if isinstance(item, int):
            out += bytes([item])
        else:
            out += item
    return out


def _lit_size(size: int) -> bytes:
    return _b(Opcode.LIT, _u32(size), Opcode.END)


def _lit_align(align: int) -> bytes:
    return _b(Opcode.LIT, _u32(align), Opcode.END)


def _build_alloc_drivers() -> bool:
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(  # noqa: S603, S607
        ["make", "-C",
         str(repo_root / "tooling/memlayout_diffharness"),
         "-s", "all"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return False
    return all(
        alloc_driver_path(a).exists() for a in ARCHES
    )


def _arch_runnable(arch: str) -> bool:
    if not alloc_driver_path(arch).exists():
        return False
    if arch != "x86_64":
        if shutil.which(f"qemu-{arch}-static") is None:
            return False
    return True


@pytest.fixture(scope="module", autouse=True)
def _ensure_drivers_built() -> None:
    _build_alloc_drivers()


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
    name: str, size: int, align: int,
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


def _hand_alloc_cases() -> list[tuple[
    str, list[MemoryRegion], int, int,
]]:
    """Each entry: (label, regions, heap_start, ram_top)."""
    return [
        ("empty",
         [], 0x1000, 0x10000),
        ("single_region",
         [_region("a", 128, 0x1000)],
         0x1000, 0x10000),
        ("alignment_padding",
         [_region("a", 128, 0x1000)],
         0x1001, 0x10000),
        ("two_packed",
         [_region("a", 128, 64), _region("b", 256, 64)],
         0x1000, 0x10000),
        ("stack_only",
         [_region(
             "stk", 0x10000, 0x1000,
             lifetime=Lifetime.STACK,
         )],
         0x1000, 0x100000),
        ("mixed",
         [
             _region("a", 128, 64),
             _region(
                 "stk", 0x1000, 0x1000,
                 lifetime=Lifetime.STACK,
             ),
             _region("b", 256, 64),
         ],
         0x1000, 0x100000),
        ("forward_crosses_reverse",
         [
             _region("hog", 0x9000, 0x1000),
             _region(
                 "stk", 0x1000, 0x1000,
                 lifetime=Lifetime.STACK,
             ),
         ],
         0x1000, 0xA000),
        ("heap_above_top",
         [], 0x10000, 0x1000),
        ("immutable_after_init",
         [_region(
             "trust", 4096, 0x1000,
             lifetime=Lifetime.IMMUTABLE_AFTER_INIT,
         )],
         0x1000, 0x10000),
        ("init_only",
         [_region(
             "scratch", 128, 64,
             lifetime=Lifetime.INIT_ONLY,
         )],
         0x1000, 0x10000),
        ("size_via_tuning_and_cpu",
         [MemoryRegion(
             name="rx_pool",
             name_hash=0xDEADBEEF,
             # tuning.rx_queue_depth ×
             #   align_up(rx_buffer_hint, l1d_line)
             size_bytecode=_b(
                 Opcode.TUNING, 0,
                 Opcode.TUNING, 2,
                 Opcode.CPU, 0,
                 Opcode.ALIGN_UP,
                 Opcode.MUL,
                 Opcode.END,
             ),
             align_bytecode=_b(
                 Opcode.CPU, 0, Opcode.END,
             ),
             owner_id=0,
             lifetime=Lifetime.STEADY_STATE,
             writable=True,
         )],
         0x1000, 0x100000),
        ("non_pow2_align",
         [MemoryRegion(
             name="bad",
             name_hash=0,
             size_bytecode=_lit_size(64),
             align_bytecode=_b(
                 Opcode.LIT, _u32(3), Opcode.END,
             ),
             owner_id=0,
             lifetime=Lifetime.STEADY_STATE,
             writable=True,
         )],
         0x1000, 0x10000),
        # Inner-VM BytecodeError surfacing through the
        # allocator: size_bc divides by literal zero →
        # BCVM_ERR_DIV_LIT_ZERO (rc=12) propagates out as
        # the allocator's rc, exercising the BytecodeError-
        # to-rc mapping path in python_alloc_verdict.
        ("inner_bc_div_zero",
         [MemoryRegion(
             name="bad_size",
             name_hash=0,
             size_bytecode=_b(
                 Opcode.LIT, _u32(64),
                 Opcode.DIV_LIT, 0,
                 Opcode.END,
             ),
             align_bytecode=_lit_align(64),
             owner_id=0,
             lifetime=Lifetime.STEADY_STATE,
             writable=True,
         )],
         0x1000, 0x100000),
    ]


@pytest.mark.parametrize("arch", ARCHES)
def test_allocator_hand_vectors_parity(
    arch: str,
    cpu: CpuCharacteristics,
    profile: TuningProfile,
) -> None:
    if not _arch_runnable(arch):
        pytest.skip(
            f"{arch} alloc driver or qemu-static unavailable"
        )
    for label, regions, heap, top in _hand_alloc_cases():
        py = python_alloc_verdict(
            regions, cpu, profile, heap, top,
        )
        asm = run_asm_alloc(
            arch, regions, cpu, profile, heap, top,
        )
        # rc, forward_end, reverse_end must match on every
        # path. The assignments tuple is only compared on
        # success: on error the contract says "output is
        # undefined / caller MUST NOT use." Python returns
        # all-zero assignments on error; asm preserves
        # whatever partial writes happened before the failure
        # — both honor the contract, neither is wrong, but
        # strict equality would over-constrain the asm side.
        py_rc, py_fwd, py_rev, py_assigns = py
        asm_rc, asm_fwd, asm_rev, asm_assigns = asm
        assert (py_rc, py_fwd, py_rev) == (
            asm_rc, asm_fwd, asm_rev,
        ), (
            f"diff on '{label}' ({arch}) headline:\n"
            f"  py  = ({py_rc}, {py_fwd}, {py_rev})\n"
            f"  asm = ({asm_rc}, {asm_fwd}, {asm_rev})"
        )
        if py_rc == 0:
            assert py_assigns == asm_assigns, (
                f"diff on '{label}' ({arch}) assignments:\n"
                f"  py  = {py_assigns}\n"
                f"  asm = {asm_assigns}"
            )
