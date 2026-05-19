"""Tests for memreq_codegen.encoding."""

from __future__ import annotations

import pytest

from memlayout.types import Opcode
from memreq_codegen.encoding import (
    ALIGN_BYTECODE_BYTES,
    OP_ALIGN_UP,
    OP_CALL_THUNK,
    OP_CPU,
    OP_DIV_LIT,
    OP_END,
    OP_LIT,
    OP_MUL,
    OP_TUNING,
    SIZE_BYTECODE_BYTES,
    Op,
    encode_bytecode,
    encode_lit_bytecode,
    fnv1a_32,
)


class TestFnv1a32:
    """FNV-1a 32-bit hash."""

    def test_empty_string_is_offset_basis(self) -> None:
        # Standard FNV-1a property: hash of empty string is the
        # offset basis.
        assert fnv1a_32("") == 0x811C9DC5

    def test_single_char_a(self) -> None:
        # Reference: FNV-1a("a") = 0xe40c292c per the canonical
        # implementation.
        assert fnv1a_32("a") == 0xE40C292C

    def test_deterministic(self) -> None:
        assert fnv1a_32("foo") == fnv1a_32("foo")

    def test_distinct_inputs_distinct_hashes(self) -> None:
        # Collisions exist but adjacent short strings shouldn't hit
        # one in practice — useful regression-canary.
        assert fnv1a_32("a") != fnv1a_32("b")

    def test_result_is_u32(self) -> None:
        # Run a few inputs through and verify the result never
        # exceeds the u32 range.
        for s in ("", "a", "ab", "smoke_test", "x" * 100):
            h = fnv1a_32(s)
            assert 0 <= h <= 0xFFFFFFFF

    def test_utf8_encoding_used(self) -> None:
        # Non-ASCII goes through utf-8 (4-byte sequence for é);
        # different from any ASCII input.
        assert fnv1a_32("é") != fnv1a_32("e")


class TestEncodeLitBytecode:
    """LIT-only bytecode encoding."""

    def test_lit_4096_in_size_buffer(self) -> None:
        out = encode_lit_bytecode(4096, SIZE_BYTECODE_BYTES)
        assert len(out) == SIZE_BYTECODE_BYTES
        # LIT (0x01) + LE u32(4096 = 0x1000) + END (0x00) + zero pad
        assert out[:6] == bytes([0x01, 0x00, 0x10, 0x00, 0x00, 0x00])
        assert all(b == 0 for b in out[6:])

    def test_lit_1_in_align_buffer(self) -> None:
        out = encode_lit_bytecode(1, ALIGN_BYTECODE_BYTES)
        assert len(out) == ALIGN_BYTECODE_BYTES
        assert out[:6] == bytes([0x01, 0x01, 0x00, 0x00, 0x00, 0x00])
        assert out[6] == 0 and out[7] == 0

    def test_lit_max_u32(self) -> None:
        out = encode_lit_bytecode(0xFFFFFFFF, SIZE_BYTECODE_BYTES)
        assert out[:6] == bytes([0x01, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])

    def test_lit_zero(self) -> None:
        out = encode_lit_bytecode(0, ALIGN_BYTECODE_BYTES)
        assert out[:6] == bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x00])

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            encode_lit_bytecode(-1, SIZE_BYTECODE_BYTES)

    def test_rejects_over_u32(self) -> None:
        with pytest.raises(ValueError):
            encode_lit_bytecode(0x100000000, SIZE_BYTECODE_BYTES)


class TestOpcodeConstantsParity:
    """Wire-level opcode constants match `memlayout.types.Opcode`.

    The constants in encoding.py are duplicated by design (see the
    module docstring) so memreq_codegen has no inbound dependency
    on memlayout. This test catches drift if either side renumbers.
    """

    def test_all_opcodes_align(self) -> None:
        assert OP_END == Opcode.END.value
        assert OP_LIT == Opcode.LIT.value
        assert OP_TUNING == Opcode.TUNING.value
        assert OP_CPU == Opcode.CPU.value
        assert OP_MUL == Opcode.MUL.value
        assert OP_DIV_LIT == Opcode.DIV_LIT.value
        assert OP_ALIGN_UP == Opcode.ALIGN_UP.value
        assert OP_CALL_THUNK == Opcode.CALL_THUNK.value


class TestEncodeBytecode:
    """Generic op-list encoder (task #28)."""

    def test_single_lit_matches_encode_lit_bytecode(self) -> None:
        # The literal shortcut must produce identical wire bytes to
        # the generic encoder; both feed the same VM.
        generic = encode_bytecode(
            [Op(OP_LIT, 4096)], SIZE_BYTECODE_BYTES,
        )
        shortcut = encode_lit_bytecode(4096, SIZE_BYTECODE_BYTES)
        assert generic == shortcut

    def test_cpu_field_id_0(self) -> None:
        # CPU opcode + u8 field id (0 picks the first
        # CpuCharacteristics field). + END + zero pad.
        out = encode_bytecode([Op(OP_CPU, 0)], SIZE_BYTECODE_BYTES)
        assert out[:3] == bytes([OP_CPU, 0x00, OP_END])
        assert all(b == 0 for b in out[3:])

    def test_tuning_field_id_5(self) -> None:
        out = encode_bytecode([Op(OP_TUNING, 5)], ALIGN_BYTECODE_BYTES)
        assert out[:3] == bytes([OP_TUNING, 0x05, OP_END])

    def test_lit_lit_mul_expression(self) -> None:
        # 524288 × 4 — literal expression: LIT 524288, LIT 4, MUL, END.
        ops = [Op(OP_LIT, 524288), Op(OP_LIT, 4), Op(OP_MUL)]
        out = encode_bytecode(ops, SIZE_BYTECODE_BYTES)
        # LIT 524288 (= 0x80000): 0x01, 0x00, 0x00, 0x08, 0x00 (5 B)
        # LIT 4:                  0x01, 0x04, 0x00, 0x00, 0x00 (5 B)
        # MUL:                    0x04                          (1 B)
        # END:                    0x00                          (1 B)
        # = 12 bytes, then 4 bytes zero pad to fill 16
        assert out[:12] == bytes([
            0x01, 0x00, 0x00, 0x08, 0x00,
            0x01, 0x04, 0x00, 0x00, 0x00,
            0x04, 0x00,
        ])
        assert all(b == 0 for b in out[12:])

    def test_cpu_mul_align_up(self) -> None:
        # CPU 1, LIT 64, MUL, LIT 4096, ALIGN_UP, END — 10 bytes.
        ops = [
            Op(OP_CPU, 1),
            Op(OP_LIT, 64),
            Op(OP_MUL),
            Op(OP_LIT, 4096),
            Op(OP_ALIGN_UP),
        ]
        out = encode_bytecode(ops, SIZE_BYTECODE_BYTES)
        assert out[:13] == bytes([
            OP_CPU, 0x01,
            OP_LIT, 0x40, 0x00, 0x00, 0x00,
            OP_MUL,
            OP_LIT, 0x00, 0x10, 0x00, 0x00,
        ]) + bytes()
        # The above shows the OPs; check ALIGN_UP + END land at +13/+14
        assert out[13] == OP_ALIGN_UP
        assert out[14] == OP_END

    def test_div_lit_payload(self) -> None:
        ops = [Op(OP_LIT, 4096), Op(OP_DIV_LIT, 64)]
        out = encode_bytecode(ops, ALIGN_BYTECODE_BYTES)
        # LIT 4096 (5B) + DIV_LIT 64 (2B) + END (1B) = 8 bytes,
        # exactly fits ALIGN_BYTECODE_BYTES.
        assert out == bytes([
            OP_LIT, 0x00, 0x10, 0x00, 0x00,
            OP_DIV_LIT, 0x40,
            OP_END,
        ])

    def test_call_thunk_u32_payload(self) -> None:
        ops = [Op(OP_CALL_THUNK, 0xDEADBEEF)]
        out = encode_bytecode(ops, SIZE_BYTECODE_BYTES)
        assert out[:6] == bytes([
            OP_CALL_THUNK, 0xEF, 0xBE, 0xAD, 0xDE, OP_END,
        ])

    def test_rejects_empty_ops(self) -> None:
        with pytest.raises(ValueError, match="empty op list"):
            encode_bytecode([], SIZE_BYTECODE_BYTES)

    def test_rejects_explicit_end_in_stream(self) -> None:
        with pytest.raises(ValueError, match="OP_END must not appear"):
            encode_bytecode([Op(OP_END)], SIZE_BYTECODE_BYTES)

    def test_rejects_unknown_opcode(self) -> None:
        with pytest.raises(ValueError, match="unknown opcode"):
            encode_bytecode([Op(0xFE, 0)], SIZE_BYTECODE_BYTES)

    def test_rejects_payload_on_payloadless(self) -> None:
        # MUL takes no payload; surfacing the bug to the caller
        # beats silently dropping the value.
        with pytest.raises(ValueError, match="takes no payload"):
            encode_bytecode([Op(OP_MUL, 1)], SIZE_BYTECODE_BYTES)

    def test_rejects_missing_payload(self) -> None:
        with pytest.raises(ValueError, match="requires a"):
            encode_bytecode([Op(OP_LIT)], SIZE_BYTECODE_BYTES)

    def test_rejects_payload_overflow_u8(self) -> None:
        with pytest.raises(ValueError, match="out of 8-bit"):
            encode_bytecode([Op(OP_CPU, 256)], SIZE_BYTECODE_BYTES)

    def test_rejects_payload_overflow_u32(self) -> None:
        with pytest.raises(ValueError, match="out of 32-bit"):
            encode_bytecode([Op(OP_LIT, 0x100000000)], SIZE_BYTECODE_BYTES)

    def test_rejects_payload_negative(self) -> None:
        with pytest.raises(ValueError, match="out of 8-bit"):
            encode_bytecode([Op(OP_DIV_LIT, -1)], ALIGN_BYTECODE_BYTES)

    def test_rejects_oversize_in_align_buffer(self) -> None:
        # A 10-op expression won't fit in 8-byte align buffer.
        ops = [Op(OP_LIT, i) for i in range(3)]
        # 3 * (1 + 4) + 1 (END) = 16 bytes, larger than 8.
        with pytest.raises(ValueError, match="exceeds buffer_bytes"):
            encode_bytecode(ops, ALIGN_BYTECODE_BYTES)
