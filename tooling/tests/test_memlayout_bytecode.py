"""Hand-authored test vectors for the bytecode interpreter.

Covers each opcode in isolation, common compositions, and every
documented failure mode. The same vectors will drive the
differential test against the per-arch assembly interpreter
(step 3): both implementations must agree on every input.
"""

import struct

import pytest

from memlayout.bytecode import BytecodeError, run_bytecode
from memlayout.models import (
    CpuCharacteristics,
    TuningProfile,
)
from memlayout.types import Opcode


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


@pytest.fixture(name="cpu")
def fixture_cpu() -> CpuCharacteristics:
    return CpuCharacteristics(
        l1d_line_bytes=64,
        l1d_bytes=32_768,
        l1i_bytes=32_768,
        l2_bytes=262_144,
        l3_bytes_per_cluster=0,
        cores_sharing_l2=1,
        cores_sharing_l3=1,
        hw_prefetcher_stride_lines=0,
        detected_model_id=0,
    )


@pytest.fixture(name="profile")
def fixture_profile() -> TuningProfile:
    return TuningProfile(
        rx_queue_depth=256,
        tx_queue_depth=256,
        rx_buffer_bytes_hint=2048,
        actor_pool_size_per_core=64,
        tls_session_cache_entries=1024,
        worker_core_count=4,
    )


def test_lit_pushes_literal(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(Opcode.LIT, _u32(0x1234ABCD), Opcode.END)
    assert run_bytecode(code, cpu, profile) == 0x1234ABCD


def test_tuning_loads_field(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # field 0 = rx_queue_depth = 256
    code = _b(Opcode.TUNING, 0, Opcode.END)
    assert run_bytecode(code, cpu, profile) == 256


def test_cpu_loads_field(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # field 0 = l1d_line_bytes = 64
    code = _b(Opcode.CPU, 0, Opcode.END)
    assert run_bytecode(code, cpu, profile) == 64


def test_mul_pops_two_pushes_product(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(
        Opcode.LIT, _u32(7),
        Opcode.LIT, _u32(11),
        Opcode.MUL,
        Opcode.END,
    )
    assert run_bytecode(code, cpu, profile) == 77


def test_div_lit(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(
        Opcode.LIT, _u32(100),
        Opcode.DIV_LIT, 4,
        Opcode.END,
    )
    assert run_bytecode(code, cpu, profile) == 25


def test_align_up_already_aligned(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(
        Opcode.LIT, _u32(64),
        Opcode.LIT, _u32(64),
        Opcode.ALIGN_UP,
        Opcode.END,
    )
    assert run_bytecode(code, cpu, profile) == 64


def test_align_up_rounds_up(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(
        Opcode.LIT, _u32(65),
        Opcode.LIT, _u32(64),
        Opcode.ALIGN_UP,
        Opcode.END,
    )
    assert run_bytecode(code, cpu, profile) == 128


def test_canonical_buffer_pool_expression(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # tuning.rx_queue_depth × align_up(rx_buffer_hint, l1d_line)
    # = 256 × align_up(2048, 64)
    # = 256 × 2048
    # = 524288
    code = _b(
        Opcode.TUNING, 0,           # rx_queue_depth = 256
        Opcode.TUNING, 2,           # rx_buffer_bytes_hint = 2048
        Opcode.CPU, 0,              # l1d_line_bytes = 64
        Opcode.ALIGN_UP,
        Opcode.MUL,
        Opcode.END,
    )
    assert run_bytecode(code, cpu, profile) == 524_288


def test_call_thunk_invokes_registered_fn(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(Opcode.CALL_THUNK, _u32(42), Opcode.END)
    thunks = {42: lambda _c, _p: 0xDEADBEEF}
    assert run_bytecode(code, cpu, profile, thunks) == 0xDEADBEEF


def test_call_thunk_unregistered_id_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(Opcode.CALL_THUNK, _u32(99), Opcode.END)
    with pytest.raises(BytecodeError, match="thunk id"):
        run_bytecode(code, cpu, profile)


def test_empty_bytecode_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    with pytest.raises(BytecodeError, match="empty"):
        run_bytecode(b"", cpu, profile)


def test_missing_end_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(Opcode.LIT, _u32(1))
    with pytest.raises(BytecodeError, match="END"):
        run_bytecode(code, cpu, profile)


def test_end_with_empty_stack_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(Opcode.END)
    with pytest.raises(BytecodeError, match="empty stack"):
        run_bytecode(code, cpu, profile)


def test_unknown_opcode_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(0x7F, Opcode.END)
    with pytest.raises(BytecodeError, match="unknown opcode"):
        run_bytecode(code, cpu, profile)


def test_truncated_lit_payload_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # LIT needs 4 bytes; only give 3.
    code = _b(Opcode.LIT, b"\x01\x02\x03")
    with pytest.raises(BytecodeError, match="truncated"):
        run_bytecode(code, cpu, profile)


def test_truncated_byte_payload_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # CPU needs 1 byte; opcode is the last byte.
    code = _b(Opcode.CPU)
    with pytest.raises(BytecodeError, match="truncated"):
        run_bytecode(code, cpu, profile)


def test_mul_underflow_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(Opcode.LIT, _u32(1), Opcode.MUL, Opcode.END)
    with pytest.raises(BytecodeError, match="underflow"):
        run_bytecode(code, cpu, profile)


def test_div_lit_zero_divisor_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(
        Opcode.LIT, _u32(10),
        Opcode.DIV_LIT, 0,
        Opcode.END,
    )
    with pytest.raises(BytecodeError, match="divisor is zero"):
        run_bytecode(code, cpu, profile)


def test_align_up_zero_align_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(
        Opcode.LIT, _u32(100),
        Opcode.LIT, _u32(0),
        Opcode.ALIGN_UP,
        Opcode.END,
    )
    with pytest.raises(BytecodeError, match="align is zero"):
        run_bytecode(code, cpu, profile)


def test_align_up_non_pow2_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(
        Opcode.LIT, _u32(100),
        Opcode.LIT, _u32(3),
        Opcode.ALIGN_UP,
        Opcode.END,
    )
    with pytest.raises(BytecodeError, match="power of two"):
        run_bytecode(code, cpu, profile)


def test_cpu_field_out_of_range_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(Opcode.CPU, 99, Opcode.END)
    with pytest.raises(BytecodeError, match="cpu field id"):
        run_bytecode(code, cpu, profile)


def test_tuning_field_out_of_range_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    code = _b(Opcode.TUNING, 99, Opcode.END)
    with pytest.raises(BytecodeError, match="tuning field id"):
        run_bytecode(code, cpu, profile)


def test_stack_overflow_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # 4-deep stack; push 5 to overflow.
    code = _b(
        Opcode.LIT, _u32(1),
        Opcode.LIT, _u32(2),
        Opcode.LIT, _u32(3),
        Opcode.LIT, _u32(4),
        Opcode.LIT, _u32(5),
        Opcode.END,
    )
    with pytest.raises(BytecodeError, match="stack overflow"):
        run_bytecode(code, cpu, profile)


def test_mul_overflow_wraps_u64(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # 2^32 × 2^32 = 2^64, which wraps to 0 in u64.
    code = _b(
        Opcode.LIT, _u32(0xFFFFFFFF),
        Opcode.LIT, _u32(0xFFFFFFFF),
        Opcode.MUL,
        Opcode.END,
    )
    expected = (0xFFFFFFFF * 0xFFFFFFFF) & ((1 << 64) - 1)
    assert run_bytecode(code, cpu, profile) == expected


def test_trailing_bytes_after_end_ignored(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # END terminates evaluation; junk past it never executes.
    code = _b(
        Opcode.LIT, _u32(7),
        Opcode.END,
        b"\xff\xff\xff",
    )
    assert run_bytecode(code, cpu, profile) == 7


def test_thunk_negative_return_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # Reaches the `value < 0` branch in push().
    code = _b(Opcode.CALL_THUNK, _u32(7), Opcode.END)
    with pytest.raises(BytecodeError, match="out of u64"):
        run_bytecode(code, cpu, profile, {7: lambda c, p: -1})


def test_thunk_huge_return_raises(
    cpu: CpuCharacteristics, profile: TuningProfile,
) -> None:
    # Reaches the `value > MAX_U64` branch in push().
    code = _b(Opcode.CALL_THUNK, _u32(7), Opcode.END)
    with pytest.raises(BytecodeError, match="out of u64"):
        run_bytecode(
            code, cpu, profile,
            {7: lambda c, p: (1 << 65)},
        )
