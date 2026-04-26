"""Driver wrapper for the per-arch bytecode VM.

Spawns the static C+asm driver under qemu-<arch>-static (or
native on x86_64), feeds it test cases on stdin, reads
(rc, result) tuples from stdout. The Python reference is the
oracle; the asm side must agree on every input.

Used by:
  tooling/tests/test_memlayout_diff_x86_64.py
  tooling/tests/test_memlayout_diff_aarch64.py

The wire format mirrors driver.c:
  per case  in:  u32 code_len; bytes; u32 cpu_count; u64[]
                 cpu_values; u32 tun_count; u64[] tun_values
  per case  out: i32 rc; u64 result
"""

import shutil
import struct
import subprocess
from collections.abc import Sequence
from pathlib import Path

from memlayout.bytecode import BytecodeError, run_bytecode
from memlayout.models import (
    CpuCharacteristics,
    MemoryRegion,
    TuningProfile,
)
from memlayout.reference import LayoutOverflow, allocate


# Maps Python BytecodeError messages → wire-level rc codes.
# Keep in lockstep with bcvm_abi.h enum bcvm_err and the
# .Lbcvm_* labels in arch/<isa>/memory/bytecode_vm.S.
ERR_OK = 0
ERR_EMPTY_BYTECODE = 1
ERR_MISSING_END = 2
ERR_END_EMPTY_STACK = 3
ERR_END_STACK_MULTI = 4
ERR_UNKNOWN_OPCODE = 5
ERR_TRUNCATED_PAYLOAD = 6
ERR_STACK_OVERFLOW = 7
ERR_STACK_UNDERFLOW = 8
ERR_VALUE_OUT_OF_U64 = 9
ERR_CPU_FIELD_OOR = 10
ERR_TUNING_FIELD_OOR = 11
ERR_DIV_LIT_ZERO = 12
ERR_ALIGN_ZERO = 13
ERR_ALIGN_NOT_POW2 = 14
ERR_MUL_OVERFLOW = 15
ERR_ALIGN_UP_OVERFLOW = 16
ERR_THUNK_UNREGISTERED = 17


_PY_TO_RC = (
    ("empty bytecode", ERR_EMPTY_BYTECODE),
    ("missing END", ERR_MISSING_END),
    ("END reached with empty stack", ERR_END_EMPTY_STACK),
    ("END reached with", ERR_END_STACK_MULTI),
    ("unknown opcode", ERR_UNKNOWN_OPCODE),
    ("truncated", ERR_TRUNCATED_PAYLOAD),
    ("stack overflow", ERR_STACK_OVERFLOW),
    ("stack underflow", ERR_STACK_UNDERFLOW),
    ("out of u64", ERR_VALUE_OUT_OF_U64),
    ("cpu field id", ERR_CPU_FIELD_OOR),
    ("tuning field id", ERR_TUNING_FIELD_OOR),
    ("DIV_LIT divisor", ERR_DIV_LIT_ZERO),
    ("ALIGN_UP align is zero", ERR_ALIGN_ZERO),
    ("not a power of two", ERR_ALIGN_NOT_POW2),
    ("MUL overflow", ERR_MUL_OVERFLOW),
    ("ALIGN_UP overflow", ERR_ALIGN_UP_OVERFLOW),
    ("thunk id", ERR_THUNK_UNREGISTERED),
)


def python_verdict(
    code: bytes,
    cpu: CpuCharacteristics,
    profile: TuningProfile,
) -> tuple[int, int]:
    """Run the Python reference and return (rc, result).

    Translates BytecodeError messages to the asm-side rc
    code via _PY_TO_RC. Any unmatched message becomes
    rc = -1 so the differential check fails loudly rather
    than silently classifying as a known error.
    """
    try:
        result = run_bytecode(code, cpu, profile)
    except BytecodeError as exc:
        msg = str(exc)
        for needle, rc in _PY_TO_RC:
            if needle in msg:
                return rc, 0
        return -1, 0  # pragma: no cover
    return ERR_OK, result


def _serialize_cpu_tun(values: Sequence[int]) -> bytes:
    out = struct.pack("<I", len(values))
    for v in values:
        out += struct.pack("<Q", v)
    return out


def serialize_case(
    code: bytes,
    cpu: CpuCharacteristics,
    profile: TuningProfile,
) -> bytes:
    """Encode one test case in the on-the-wire format."""
    cpu_vals = tuple(
        getattr(cpu, name)
        for name in cpu.__class__.model_fields.keys()
    )
    tun_vals = tuple(
        getattr(profile, name)
        for name in profile.__class__.model_fields.keys()
    )
    return (
        struct.pack("<I", len(code))
        + code
        + _serialize_cpu_tun(cpu_vals)
        + _serialize_cpu_tun(tun_vals)
    )


def driver_path(arch: str) -> Path:
    """Return the per-arch driver binary path.

    Layout: tooling/memlayout_diffharness/build/<arch>/
            bcvm_driver. Caller is responsible for invoking
            `make -C tooling/memlayout_diffharness all` if
            the binary is missing.
    """
    repo_root = Path(__file__).resolve().parents[3]
    return (
        repo_root
        / "tooling/memlayout_diffharness/build"
        / arch / "bcvm_driver"
    )


def driver_command(arch: str) -> list[str]:
    """Return the command (with optional QEMU prefix) to
    invoke the per-arch driver from this host.
    """
    binary = str(driver_path(arch))
    if arch == "x86_64":
        return [binary]
    qemu = shutil.which(f"qemu-{arch}-static")
    if qemu is None:  # pragma: no cover
        # Reachable on a host without the cross-arch QEMU
        # static binaries installed; the test harness
        # pre-checks via _arch_runnable and skips before
        # ever calling this.
        raise RuntimeError(
            f"qemu-{arch}-static not found in PATH"
        )
    return [qemu, binary]


def run_asm_cases(
    arch: str,
    cases: Sequence[
        tuple[bytes, CpuCharacteristics, TuningProfile]
    ],
) -> list[tuple[int, int]]:
    """Spawn the driver once and feed all cases through it.

    Returns a parallel list of (rc, result) tuples — one per
    input case. Single fork-and-stream amortizes the QEMU
    startup cost across N cases instead of paying it per case.
    """
    payload = b"".join(
        serialize_case(c, cpu, prof) for c, cpu, prof in cases
    )
    cmd = driver_command(arch)
    proc = subprocess.run(  # noqa: S603 — args are own data
        cmd,
        input=payload,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:  # pragma: no cover
        # Reachable on a driver crash or QEMU error.
        # Tested-in-spirit by the asm side itself; raising
        # here surfaces the error as a clear test failure
        # rather than parsing garbage output.
        raise RuntimeError(
            f"{cmd[0]} returncode {proc.returncode}: "
            f"{proc.stderr!r}"
        )
    expected_len = len(cases) * 12
    if len(proc.stdout) != expected_len:  # pragma: no cover
        # Reachable if the driver crashes mid-stream or the
        # protocol drifts. Same surface-clearly rationale.
        raise RuntimeError(
            f"driver stdout {len(proc.stdout)} bytes, "
            f"expected {expected_len}"
        )
    return [
        struct.unpack("<iQ", proc.stdout[i:i + 12])
        for i in range(0, expected_len, 12)
    ]


# ---- Allocator differential support ------------------------------

# Allocator-side rc codes (matches enum memlayout_err in
# bcvm_abi.h). The 1..17 range from the bytecode VM is reused
# verbatim when an inner-VM evaluation fails inside a record's
# size or alignment expression.
ALLOC_OK = 0
ALLOC_ERR_OVERFLOW = 100
ALLOC_ERR_HEAP_TOP = 101
ALLOC_ERR_BAD_LIFETIME = 102

MEMREQ_RECORD_BYTES = 48
SIZE_BC_BYTES = 16
ALIGN_BC_BYTES = 8


def serialize_record(region: "MemoryRegion") -> bytes:
    """Encode a MemoryRegion to its 48-byte wire form."""
    size_bc = region.size_bytecode.ljust(SIZE_BC_BYTES, b"\x00")
    align_bc = region.align_bytecode.ljust(
        ALIGN_BC_BYTES, b"\x00",
    )
    return (
        struct.pack("<I", region.name_hash)
        + size_bc
        + align_bc
        + struct.pack(
            "<HBB",
            region.owner_id, int(region.lifetime),
            int(region.writable),
        )
        + struct.pack("<QQ", 0, 0)
    )


def parse_record(blob: bytes) -> tuple[int, int]:
    """Read the assigned (addr, size) pair from a record."""
    return struct.unpack("<QQ", blob[32:48])


def serialize_alloc_case(
    regions: "list[MemoryRegion]",
    cpu: CpuCharacteristics,
    profile: TuningProfile,
    heap_start: int,
    ram_top: int,
) -> bytes:
    """Encode one allocator test case."""
    cpu_vals = tuple(
        getattr(cpu, name)
        for name in cpu.__class__.model_fields.keys()
    )
    tun_vals = tuple(
        getattr(profile, name)
        for name in profile.__class__.model_fields.keys()
    )
    payload = struct.pack("<I", len(regions))
    for region in regions:
        payload += serialize_record(region)
    payload += _serialize_cpu_tun(cpu_vals)
    payload += _serialize_cpu_tun(tun_vals)
    payload += struct.pack("<QQ", heap_start, ram_top)
    return payload


def alloc_driver_path(arch: str) -> Path:
    """Per-arch allocator driver binary path."""
    repo_root = Path(__file__).resolve().parents[3]
    return (
        repo_root
        / "tooling/memlayout_diffharness/build"
        / arch / "alloc_driver"
    )


def alloc_driver_command(arch: str) -> list[str]:
    binary = str(alloc_driver_path(arch))
    if arch == "x86_64":
        return [binary]
    qemu = shutil.which(f"qemu-{arch}-static")
    if qemu is None:  # pragma: no cover
        raise RuntimeError(
            f"qemu-{arch}-static not found in PATH"
        )
    return [qemu, binary]


def run_asm_alloc(
    arch: str,
    regions: "list[MemoryRegion]",
    cpu: CpuCharacteristics,
    profile: TuningProfile,
    heap_start: int,
    ram_top: int,
) -> tuple[int, int, int, list[tuple[int, int]]]:
    """Run one allocator case through the per-arch asm driver.

    Returns (rc, forward_end, reverse_end, [(addr, size), ...])
    where the assigned tuples are in record order (one per
    input region; (0, 0) entries indicate skipped lifetimes
    or unreached records on error).
    """
    payload = serialize_alloc_case(
        regions, cpu, profile, heap_start, ram_top,
    )
    cmd = alloc_driver_command(arch)
    proc = subprocess.run(  # noqa: S603 — args are own data
        cmd, input=payload, capture_output=True,
        check=False, timeout=120,
    )
    if proc.returncode != 0:  # pragma: no cover
        raise RuntimeError(
            f"{cmd[0]} returncode {proc.returncode}: "
            f"{proc.stderr!r}"
        )
    expected_len = (
        4 + 8 + 8 + len(regions) * MEMREQ_RECORD_BYTES
    )
    if len(proc.stdout) != expected_len:  # pragma: no cover
        raise RuntimeError(
            f"alloc driver stdout {len(proc.stdout)} bytes, "
            f"expected {expected_len}"
        )
    rc, forward_end, reverse_end = struct.unpack(
        "<iQQ", proc.stdout[:20],
    )
    assignments = []
    for i in range(len(regions)):
        offset = 20 + i * MEMREQ_RECORD_BYTES
        rec = proc.stdout[offset:offset + MEMREQ_RECORD_BYTES]
        assignments.append(parse_record(rec))
    return rc, forward_end, reverse_end, assignments


def _zero_assigns(
    regions: "list[MemoryRegion]",
) -> list[tuple[int, int]]:
    return [(0, 0) for _ in regions]


def _alloc_error_verdict(
    exc: Exception,
    regions: "list[MemoryRegion]",
) -> tuple[int, int, int, list[tuple[int, int]]]:
    """Map a Python-side allocator exception to wire-rc form."""
    msg = str(exc)
    if isinstance(exc, LayoutOverflow):
        if "heap_start" in msg:
            rc = ALLOC_ERR_HEAP_TOP
        else:
            rc = ALLOC_ERR_OVERFLOW
        return rc, 0, 0, _zero_assigns(regions)
    # BytecodeError — translate via the existing substring map.
    for needle, rc in _PY_TO_RC:
        if needle in msg:
            return rc, 0, 0, _zero_assigns(regions)
    return -1, 0, 0, _zero_assigns(regions)  # pragma: no cover


def python_alloc_verdict(
    regions: "list[MemoryRegion]",
    cpu: CpuCharacteristics,
    profile: TuningProfile,
    heap_start: int,
    ram_top: int,
) -> tuple[int, int, int, list[tuple[int, int]]]:
    """Python reference allocator verdict in wire-comparable form.

    Returns the same tuple shape as run_asm_alloc so the test
    harness can compare element-wise.
    """
    try:
        layout = allocate(
            regions, cpu, profile,
            heap_start=heap_start, ram_top=ram_top,
        )
    except (LayoutOverflow, BytecodeError) as exc:
        return _alloc_error_verdict(exc, regions)
    by_name = {
        a.name: (a.addr, a.size) for a in layout.assignments
    }
    assignments = [by_name[r.name] for r in regions]
    return (
        ALLOC_OK,
        layout.forward_bump_end,
        layout.reverse_bump_end,
        assignments,
    )
