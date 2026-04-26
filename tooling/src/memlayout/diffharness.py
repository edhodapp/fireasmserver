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
    TuningProfile,
)


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
