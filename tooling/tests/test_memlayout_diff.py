"""Differential tests: per-arch asm bytecode VM vs Python reference.

Spawns the C+asm driver (under qemu-<arch>-static for non-native
arches; native for x86_64 on this laptop) and asserts the asm
side produces the same (rc, result) tuple as the Python
reference for every test case.

Two test functions per arch:
  - hand-vector parity: ~30 deterministic cases covering each
    opcode + every documented failure mode.
  - Hypothesis-driven random parity: 100 random
    (bytecode, cpu, tun) triples; differential agreement on
    every one.

Skips cleanly when the driver binary is missing OR when
qemu-<arch>-static isn't installed for the non-native arch.
"""

import shutil
import struct
import subprocess
from pathlib import Path

from hypothesis import given, settings, strategies as st
import pytest

from memlayout.diffharness import (
    driver_path,
    python_verdict,
    run_asm_cases,
)
from memlayout.models import (
    CpuCharacteristics,
    TuningProfile,
)
from memlayout.types import Opcode

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


def _build_drivers() -> bool:
    """Build both per-arch drivers if not already present.

    Returns True if both binaries exist after the build,
    False if a tool (gcc / aarch64-linux-gnu-gcc / nasm) is
    missing and so the build couldn't run.
    """
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(  # noqa: S603, S607
        ["make", "-C",
         str(repo_root / "tooling/memlayout_diffharness"),
         "-s", "all"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return False
    return all(driver_path(a).exists() for a in ARCHES)


def _arch_runnable(arch: str) -> bool:
    """Skip predicate for one arch's tests."""
    if not driver_path(arch).exists():
        return False
    if arch != "x86_64":
        if shutil.which(f"qemu-{arch}-static") is None:
            return False
    return True


@pytest.fixture(scope="module", autouse=True)
def _ensure_drivers_built() -> None:
    _build_drivers()


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


# Hand-authored differential vectors. Mirror the bytecode test
# file but stop at vectors the asm side actually supports
# (CALL_THUNK with a registered thunk is excluded — the asm
# side doesn't run thunks).
#
# Includes regression vectors from Gemini's 2026-04-26 HIGH
# review on commit f10ca0f's first attempt: the x86_64 stack
# layout that put rdx and rsi as slot-1 / slot-2 silently
# corrupted any stack content at depth ≥ 3 across MUL/DIV.
# Random Hypothesis bytes don't reliably construct the
# depth-3-with-deep-slot-still-needed shape, so an explicit
# vector pins it. Both arches must keep deep-slot values
# intact across MUL and DIV_LIT.
def _hand_vectors() -> list[tuple[bytes, str]]:
    return [
        (_b(Opcode.LIT, _u32(0x1234ABCD), Opcode.END),
         "lit"),
        (_b(Opcode.TUNING, 0, Opcode.END), "tuning"),
        (_b(Opcode.CPU, 0, Opcode.END), "cpu"),
        (_b(Opcode.LIT, _u32(7), Opcode.LIT, _u32(11),
            Opcode.MUL, Opcode.END),
         "mul"),
        (_b(Opcode.LIT, _u32(100), Opcode.DIV_LIT, 4,
            Opcode.END),
         "div_lit"),
        (_b(Opcode.LIT, _u32(64), Opcode.LIT, _u32(64),
            Opcode.ALIGN_UP, Opcode.END),
         "align_up_already"),
        (_b(Opcode.LIT, _u32(65), Opcode.LIT, _u32(64),
            Opcode.ALIGN_UP, Opcode.END),
         "align_up_rounds"),
        (_b(Opcode.TUNING, 0,
            Opcode.TUNING, 2,
            Opcode.CPU, 0,
            Opcode.ALIGN_UP, Opcode.MUL,
            Opcode.END),
         "buffer_pool_canonical"),
        (b"", "empty"),
        (_b(Opcode.LIT, _u32(1)), "missing_end"),
        (_b(Opcode.END), "end_empty_stack"),
        (_b(Opcode.LIT, _u32(1), Opcode.LIT, _u32(2),
            Opcode.END),
         "end_two_elements"),
        (_b(0x7F, Opcode.END), "unknown_op"),
        (_b(Opcode.LIT, b"\x01\x02\x03"),
         "truncated_lit_payload"),
        (_b(Opcode.CPU), "truncated_byte_payload"),
        (_b(Opcode.LIT, _u32(1), Opcode.MUL, Opcode.END),
         "mul_underflow"),
        (_b(Opcode.LIT, _u32(10), Opcode.DIV_LIT, 0,
            Opcode.END),
         "div_zero"),
        (_b(Opcode.LIT, _u32(100), Opcode.LIT, _u32(0),
            Opcode.ALIGN_UP, Opcode.END),
         "align_zero"),
        (_b(Opcode.LIT, _u32(100), Opcode.LIT, _u32(3),
            Opcode.ALIGN_UP, Opcode.END),
         "align_not_pow2"),
        (_b(Opcode.CPU, 99, Opcode.END), "cpu_oor"),
        (_b(Opcode.TUNING, 99, Opcode.END), "tuning_oor"),
        (_b(Opcode.LIT, _u32(1), Opcode.LIT, _u32(2),
            Opcode.LIT, _u32(3), Opcode.LIT, _u32(4),
            Opcode.LIT, _u32(5), Opcode.END),
         "stack_overflow"),
        (_b(Opcode.LIT, _u32(0xFFFFFFFF),
            Opcode.LIT, _u32(0xFFFFFFFF),
            Opcode.LIT, _u32(0xFFFFFFFF),
            Opcode.MUL, Opcode.MUL, Opcode.END),
         "mul_overflow"),
        (_b(Opcode.LIT, _u32(0xFFFFFFFF),
            Opcode.LIT, _u32(0x80000000),
            Opcode.MUL, Opcode.END),
         "mul_below_threshold"),
        (_b(Opcode.CALL_THUNK, _u32(7), Opcode.END),
         "call_thunk_unregistered"),

        # Regression: Gemini HIGH 2026-04-26 — push 4 distinct
        # values to fill stack, MUL pops top two and pushes
        # one; the 2 remaining at the BOTTOM of the stack
        # must survive the MUL. Then MUL again uses one of
        # those preserved bottom values; if it had been
        # silently zero-clobbered by an earlier mul's rdx
        # write, the second product would differ from the
        # Python reference.
        # Stack history:
        #   push 7, 11, 13, 17  → [17,13,11,7]
        #   MUL                 → [17*13, 11, 7]
        #   MUL                 → [17*13*11, 7]
        #   MUL                 → [17*13*11*7]
        # Every value at depth>=3 must round-trip through
        # the first MUL untouched.
        (_b(Opcode.LIT, _u32(7),
            Opcode.LIT, _u32(11),
            Opcode.LIT, _u32(13),
            Opcode.LIT, _u32(17),
            Opcode.MUL,
            Opcode.MUL,
            Opcode.MUL,
            Opcode.END),
         "mul_preserves_deep_stack"),

        # Same shape, DIV_LIT instead — the divisor ABI on
        # x86_64 used to alias rsi (slot 2) before the
        # 2026-04-26 reorg.
        # push 100, 200, 300, 400 → [400,300,200,100]
        # DIV_LIT 4               → [100, 300, 200, 100]
        # MUL                     → [100*300, 200, 100]
        # MUL                     → [100*300*200, 100]
        # MUL                     → [100*300*200*100]
        (_b(Opcode.LIT, _u32(100),
            Opcode.LIT, _u32(200),
            Opcode.LIT, _u32(300),
            Opcode.LIT, _u32(400),
            Opcode.DIV_LIT, 4,
            Opcode.MUL,
            Opcode.MUL,
            Opcode.MUL,
            Opcode.END),
         "div_lit_preserves_deep_stack"),
    ]


@pytest.mark.parametrize("arch", ARCHES)
def test_hand_vectors_parity(
    arch: str,
    cpu: CpuCharacteristics,
    profile: TuningProfile,
) -> None:
    if not _arch_runnable(arch):
        pytest.skip(
            f"{arch} driver or qemu-static unavailable"
        )
    vectors = _hand_vectors()
    cases = [(v[0], cpu, profile) for v in vectors]
    asm_results = run_asm_cases(arch, cases)
    for (code, name), (asm_rc, asm_result) in zip(
        vectors, asm_results, strict=True,
    ):
        py_rc, py_result = python_verdict(code, cpu, profile)
        assert (asm_rc, asm_result) == (py_rc, py_result), (
            f"diff on '{name}' ({arch}): "
            f"py=({py_rc},{py_result}) "
            f"asm=({asm_rc},{asm_result}) "
            f"code={code.hex()}"
        )


@given(st.binary(min_size=1, max_size=24))
@settings(max_examples=100, deadline=None)
def test_random_bytes_x86_64_parity(blob: bytes) -> None:
    if not _arch_runnable("x86_64"):
        pytest.skip("x86_64 driver unavailable")
    cpu = CpuCharacteristics(
        l1d_line_bytes=64, l1d_bytes=32_768, l1i_bytes=32_768,
        l2_bytes=262_144, l3_bytes_per_cluster=0,
        cores_sharing_l2=1, cores_sharing_l3=1,
        hw_prefetcher_stride_lines=0, detected_model_id=0,
    )
    profile = TuningProfile(
        rx_queue_depth=256, tx_queue_depth=256,
        rx_buffer_bytes_hint=2048, actor_pool_size_per_core=64,
        tls_session_cache_entries=1024, worker_core_count=4,
    )
    py_rc, py_result = python_verdict(blob, cpu, profile)
    asm_results = run_asm_cases(
        "x86_64", [(blob, cpu, profile)],
    )
    asm_rc, asm_result = asm_results[0]
    assert (asm_rc, asm_result) == (py_rc, py_result), (
        f"random-bytes diff (x86_64): "
        f"py=({py_rc},{py_result}) "
        f"asm=({asm_rc},{asm_result}) "
        f"code={blob.hex()}"
    )


@given(st.binary(min_size=1, max_size=24))
@settings(max_examples=100, deadline=None)
def test_random_bytes_aarch64_parity(blob: bytes) -> None:
    if not _arch_runnable("aarch64"):
        pytest.skip(
            "aarch64 driver or qemu-aarch64-static unavailable"
        )
    cpu = CpuCharacteristics(
        l1d_line_bytes=64, l1d_bytes=32_768, l1i_bytes=32_768,
        l2_bytes=262_144, l3_bytes_per_cluster=0,
        cores_sharing_l2=1, cores_sharing_l3=1,
        hw_prefetcher_stride_lines=0, detected_model_id=0,
    )
    profile = TuningProfile(
        rx_queue_depth=256, tx_queue_depth=256,
        rx_buffer_bytes_hint=2048, actor_pool_size_per_core=64,
        tls_session_cache_entries=1024, worker_core_count=4,
    )
    py_rc, py_result = python_verdict(blob, cpu, profile)
    asm_results = run_asm_cases(
        "aarch64", [(blob, cpu, profile)],
    )
    asm_rc, asm_result = asm_results[0]
    assert (asm_rc, asm_result) == (py_rc, py_result), (
        f"random-bytes diff (aarch64): "
        f"py=({py_rc},{py_result}) "
        f"asm=({asm_rc},{asm_result}) "
        f"code={blob.hex()}"
    )
